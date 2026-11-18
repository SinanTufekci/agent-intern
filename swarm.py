"""Parallel agy swarm: run N Antigravity (Gemini) workers truly concurrently.

Each worker runs in its OWN isolated HOME/USERPROFILE temp dir, so agy's
per-process state (brain/, cache/, last_conversations.json) never collides. This
is why the single-agent path in server.py must serialize via _AGY_LOCK, but the
swarm does NOT need the lock: isolated state means no race. cwd is set to each
worker's real workspace, so file access there is unchanged; HOME redirection
isolates state only.

AUTH IS NOT ALWAYS HOME-INDEPENDENT — the assumption this module shipped with
("agy reads auth from the OS credential store, not ~/.gemini") was verified on
Windows and is FALSE on macOS. Windows Credential Manager is per-user and
unaffected by $HOME, so an isolated worker still authenticates (re-verified on agy
1.1.12: `agy models` inside a fake HOME returns the full list). On macOS the login
keychain is resolved through $HOME, so a redirected HOME hides the stored
credentials, agy falls back to starting a fresh OAuth flow, and every antigravity
worker dies at the 60 s "authentication timed out" while the non-swarm tools --
which never touch HOME -- keep working. Reported as issue #2 (macOS 15 arm64, agy
1.1.11); reproduced by inspection, not on a Mac.

So isolation is now CONDITIONAL, decided by isolation_ok() two ways:

* proactively, by probing once per process (`agy -p "/usage"` inside a throwaway
  isolated HOME -- free on agy 1.1.11+, but real auth is required to answer it),
  and
* reactively, because a probe is a proxy and this one cannot be tested on the
  platform it exists for: any worker that fails with an authentication signature
  flips the process to serialized mode and RETRIES itself there, so the user gets
  an answer even if the probe was wrong or unavailable.

In serialized mode the antigravity workers run through server._run_agy in the
user's real HOME under _AGY_LOCK -- correct everywhere, at the cost of the
parallelism this module exists for (other backends are unaffected and stay
parallel). Set AGY_BRIDGE_NO_HOME_ISOLATION=1 to force it without a probe.

SECURITY: a swarm runs N unsandboxed agy agents at once — N times the
prompt-injection "lethal trifecta" surface described in server.py's module
docstring. Only use it with trusted prompts on trusted content.

Pure helpers and config are reused from server.py via lazy imports (inside
functions) so this module can be imported by server.py without a circular import.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional, Union

import proc_tree
import stats

DEFAULT_MAX_CONCURRENCY = 4
_REAL_SETTINGS = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"

# Text decoding for the swarm workers' subprocesses: the backends emit UTF-8,
# which bare text=True would decode with the Windows locale codepage
# (cp1254/cp1252) and mangle on any non-ASCII answer. Mirrors server._TEXT.
_TEXT = {"encoding": "utf-8", "errors": "replace"}


# ----------------------------------------------------------------------------- isolation
def _make_isolated_home() -> Path:
    """Fresh temp HOME seeded with settings.json (to keep the same model)."""
    home = Path(tempfile.mkdtemp(prefix="agy_swarm_"))
    dst = home / ".gemini" / "antigravity-cli"
    dst.mkdir(parents=True, exist_ok=True)
    if _REAL_SETTINGS.exists():
        try:
            (dst / "settings.json").write_text(_REAL_SETTINGS.read_text("utf-8"), "utf-8")
        except OSError:
            pass
    return home


def _env_for_home(home: Path) -> dict:
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    if os.name == "nt":
        drive, _, rest = str(home).partition(":")
        env["HOMEDRIVE"] = drive + ":"
        env["HOMEPATH"] = rest or "\\"
    return env


# Signatures of agy hitting its interactive sign-in flow instead of stored
# credentials. Matched against a FAILED worker's error text (agy's stderr tail),
# never against an answer, so a prompt that merely discusses logins can't trip it.
# From issue #2's report: "...&state=... / Waiting for authentication (timeout
# 60s)... / Or, paste the authorization code here and press Enter: / Error:
# authentication timed out."
_AUTH_FAILURE_MARKERS = (
    "authentication timed out",
    "waiting for authentication",
    "paste the authorization code",
    "not authenticated",
    "please sign in",
)

# Whether a worker in an isolated HOME can still see agy's stored auth. None until
# resolved; then cached for the process (a probe result, or a worker's own
# authentication failure). See the module docstring.
_ISOLATION_OK: Optional[bool] = None
_ISOLATION_LOCK = threading.Lock()


def _looks_like_auth_failure(text: Optional[str]) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _AUTH_FAILURE_MARKERS)


def _credential_store_follows_home() -> bool:
    """Can redirecting HOME hide agy's stored credentials on this platform?

    False on Windows: Credential Manager is keyed to the user session, not to a
    path, so an isolated worker still authenticates — verified live on agy 1.1.12
    that `agy models` inside a fake HOME returns the full list. True elsewhere,
    because macOS resolves the login keychain through $HOME (issue #2) and the
    bridge has no evidence either way for Linux's keyring. Its own function so the
    probe's platform rule is testable without patching os.name, which would also
    re-point pathlib at the wrong flavour.
    """
    return os.name != "nt"


def _probe_isolated_auth() -> Optional[bool]:
    """Can agy authenticate inside an isolated HOME? None = couldn't tell.

    The probe is `agy -p "/usage"` in a throwaway isolated HOME: on agy 1.1.11+ the
    CLI answers it itself (no agent turn, no quota, no conversation left behind)
    but the quota table comes from the account, so it cannot be answered without
    working credentials — which is exactly the property we need to test. Below
    1.1.11 there is no free way to ask, hence None.

    Only DEFINITE answers are returned. A timeout means agy sat in its 60 s
    interactive sign-in wait until we killed it (the issue #2 symptom), and an
    auth signature in its output says the same in words; both are False. A parsed
    quota table is True. Anything else — a network blip, an unexpected non-zero
    exit — is None rather than False: the reactive fallback catches a real auth
    failure anyway, so an ambiguous probe must not cost every user their
    parallelism.

    Skipped on Windows, where the credential store is per-user and HOME-independent
    by design — verified live on 1.1.12 that `agy models` inside a fake HOME returns
    the full list — so the probe would spend seconds per process to answer a
    question that cannot fail there. If that ever changes, the reactive path covers
    it.
    """
    import server

    if not _credential_store_follows_home():
        return None
    if not server.supports_print_usage():
        return None
    home = _make_isolated_home()
    try:
        proc = proc_tree.run_captured(
            [server.AGY_BIN, "--print-timeout", "20s", "-p", "/usage"],
            starts_agent=False,  # a probe, not an agent run
            env=_env_for_home(home),
            stdin=subprocess.DEVNULL,
            **_TEXT,
            timeout=25,
            **server._spawn_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return False  # stuck in agy's interactive sign-in wait
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if _looks_like_auth_failure((proc.stderr or "") + (proc.stdout or "")):
        return False
    if proc.returncode != 0:
        return None
    return True if server._parse_usage_rows(proc.stdout or "") else None


def isolation_ok() -> bool:
    """Whether antigravity workers may run in isolated HOMEs (i.e. in parallel).

    False routes them through the serialized fallback instead. Resolved once per
    process: the env override, then the probe, defaulting to True when the probe
    can't tell — the pre-existing behaviour, with the reactive fallback as the net.
    """
    global _ISOLATION_OK
    import server

    with _ISOLATION_LOCK:
        if _ISOLATION_OK is None:
            if server._env_truthy("AGY_BRIDGE_NO_HOME_ISOLATION"):
                _ISOLATION_OK = False
            else:
                probed = _probe_isolated_auth()
                _ISOLATION_OK = True if probed is None else probed
        return _ISOLATION_OK


def _mark_isolation_broken(reason: str) -> None:
    """Latch serialized mode after a worker hit agy's sign-in flow."""
    global _ISOLATION_OK
    with _ISOLATION_LOCK:
        if _ISOLATION_OK is not False:
            _ISOLATION_OK = False
            import server

            server.log.warning(
                "agy could not authenticate in an isolated HOME (%s); running "
                "antigravity swarm workers serialized in the real HOME instead",
                reason[:200],
            )


def _isolated_brain(home: Path) -> Path:
    return home / ".gemini" / "antigravity-cli" / "brain"


def _only_conv(home: Path) -> Optional[str]:
    """The conversation id in an isolated HOME — exactly one per worker run.

    If more than one somehow exists, pick the newest by mtime (defensive)."""
    brain = _isolated_brain(home)
    if not brain.exists():
        return None
    dirs = [c for c in brain.iterdir() if c.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda c: c.stat().st_mtime).name


def _iso_transcript_entries(home: Path, conv_id: str) -> list[dict]:
    """All parsed JSONL entries for a worker's isolated conversation, or []."""
    transcript = _isolated_brain(home) / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    if not transcript.exists():
        return []
    out: list[dict] = []
    for line in transcript.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _read_isolated_response(home: Path, conv_id: str) -> str:
    """Final planner answer, read from the isolated HOME's brain dir (not the real one)."""
    chunks = [
        e["content"]
        for e in _iso_transcript_entries(home, conv_id)
        if e.get("source") == "MODEL"
        and e.get("status") == "DONE"
        and e.get("type") == "PLANNER_RESPONSE"
        and e.get("content")
    ]
    if not chunks:
        raise RuntimeError("no completed MODEL response in transcript")
    return chunks[-1]


def _print_timeout_result(index, timeout_s, stdout, home, t0, workspace) -> WorkerResult:
    """The failed result for a worker agy cut off at --print-timeout.

    agy 1.2.0+ exits 0 on that, so without this the worker would report the partial
    answer as a success — see server._print_timeout_error. Callers RETURN this rather
    than raise it: the message carries the model's partial answer, and their except
    handlers match exception text against _AUTH_FAILURE_MARKERS, which must never
    see an answer.
    """
    import server

    partial = (stdout or "").strip()
    if not partial:
        conv = _only_conv(home)
        try:
            partial = _read_isolated_response(home, conv) if conv else ""
        except RuntimeError:
            pass
    return WorkerResult(
        index,
        False,
        error=str(server._print_timeout_error(timeout_s, partial)),
        elapsed=round(time.time() - t0, 1),
        workspace=workspace,
    )


class _Feed:
    """Pumps a worker's isolated transcript into its watch channel as step events."""

    def __init__(self, home: Path, index: int, start: float):
        self._home, self._index, self._start = home, index, start
        self._conv: Optional[str] = None
        self._cursor = 0

    def pump(self) -> None:
        import server
        import swarm_watch

        if self._conv is None:
            self._conv = _only_conv(self._home)
            if self._conv is None:
                return
            self._cursor = 0
        entries = _iso_transcript_entries(self._home, self._conv)
        new = []
        for entry in entries[self._cursor :]:
            for kind, text in server._entry_to_watch_lines(entry):
                new.append({"kind": kind, "text": text, "t": round(time.time() - self._start, 1)})
        self._cursor = max(self._cursor, len(entries))
        if new:
            swarm_watch.worker_append(self._index, new)


# ----------------------------------------------------------------------------- results
@dataclass
class WorkerResult:
    index: int
    ok: bool
    answer: Optional[str] = None
    error: Optional[str] = None
    elapsed: float = 0.0
    workspace: str = ""
    image_path: Optional[str] = None
    image_format: Optional[str] = None
    image_size: Optional[int] = None
    backend: str = ""


ProgressCallback = Callable[[int, int], None]


def _notify(on_progress: Optional[ProgressCallback], done: int, total: int) -> None:
    """Tell a progress listener how many of `total` workers have finished. A listener
    that raises must not fail a swarm, so its errors are dropped."""
    if on_progress is None:
        return
    try:
        on_progress(done, total)
    except Exception:  # noqa: BLE001 - progress is cosmetic; never break the run
        pass


def _submit_in_context(ex: ThreadPoolExecutor, fn: Callable, *args: Any):
    """ex.submit(fn, *args) in a copy of the caller's context. Pool threads do not
    inherit contextvars, and proc_tree.CURRENT_CALL (which lets a cancelled tool call
    find the agent processes it started) is one. Each submit takes its own copy: a
    Context cannot be entered by two threads at once."""
    return ex.submit(contextvars.copy_context().run, fn, *args)


def _normalize_workspaces(n: int, workspaces: Union[None, str, list]) -> list[str]:
    """Per-worker workspace list aligned to n prompts.

    None -> cwd for all; str -> same dir for all; list -> per-worker (len must == n)."""
    if workspaces is None:
        return [os.getcwd()] * n
    if isinstance(workspaces, str):
        return [proc_tree.resolve_workspace(workspaces)] * n
    if len(workspaces) != n:
        raise ValueError(f"workspaces length {len(workspaces)} != prompts {n}")
    return [proc_tree.resolve_workspace(w) for w in workspaces]


def _labels(prompts: list[str]) -> list[str]:
    out = []
    for p in prompts:
        line = p.strip().splitlines()[0] if p.strip() else "(empty)"
        out.append(line[:120] + ("…" if len(line) > 120 else ""))
    return out


def _basename_any(path: str) -> str:
    """Last path component, handling both '/' and '\\' regardless of host OS.

    os.path.basename uses only the host's separator, so on Linux a Windows-style
    workspace ("C:\\a\\b\\repo") wouldn't be shortened (and a POSIX path on Windows
    likewise). Splitting on both separators keeps worker labels clean everywhere.
    """
    cleaned = path.replace("\\", "/").rstrip("/")
    return cleaned.rsplit("/", 1)[-1] or path


def _repos(workspaces: list[str]) -> list[str]:
    return [_basename_any(w) for w in workspaces]


def _watch_models(tasks: list[dict]) -> list[str]:
    """The model each dashboard row shows: the task's own `model`, else for an
    antigravity worker the settings.json model it will run on, else "" (no guess:
    the other backends' defaults are not known to the bridge)."""
    import server

    default = server._agy_settings_model() or ""
    return [t.get("model") or (default if t.get("backend") == "antigravity" else "") for t in tasks]


# ----------------------------------------------------------------------------- text swarm
def _run_text_worker_serialized(
    index, prompt, workspace, model, timeout_s, t0, plan=False
) -> WorkerResult:
    """One antigravity worker WITHOUT HOME isolation, serialized behind _AGY_LOCK.

    The fallback for a platform where isolating HOME hides agy's credentials (see
    the module docstring). It delegates to server._run_agy, which owns the lock and
    the conversation-resolution logic for the real state dir — the isolated
    readers here (_only_conv, _read_isolated_response) assume a HOME holding
    exactly one conversation and cannot work against the user's real brain dir.
    `pin=False` keeps a worker from claiming the workspace's continue slot.

    Workers run one at a time in this mode, so a swarm takes about as long as the
    same tasks run back to back.
    """
    import server

    try:
        ans = server._run_agy(prompt, workspace, False, timeout_s, model, plan, pin=False)
        return WorkerResult(
            index, True, answer=ans, elapsed=round(time.time() - t0, 1), workspace=workspace
        )
    except Exception as e:  # noqa: BLE001 — error isolation: one worker must not sink the swarm
        return WorkerResult(
            index, False, error=str(e), elapsed=round(time.time() - t0, 1), workspace=workspace
        )


def _run_text_worker(index, prompt, workspace, model, timeout_s, plan=False) -> WorkerResult:
    import server

    if not isolation_ok():
        return _run_text_worker_serialized(
            index, prompt, workspace, model, timeout_s, time.time(), plan
        )
    home = _make_isolated_home()
    t0 = time.time()
    try:
        args = server._agy_base_args(timeout_s, plan)
        if model:
            args += ["--model", model]
        args += ["-p", prompt]
        proc = proc_tree.run_captured(
            args,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            env=_env_for_home(home),
            **_TEXT,
            timeout=timeout_s + 30,
            **server._spawn_kwargs(),
        )
        # The timeout first: agy 1.5.0+ exits 3 on it, 1.2.0-1.4.x exited 0.
        if server._print_timed_out(proc.stderr):
            return _print_timeout_result(index, timeout_s, proc.stdout, home, t0, workspace)
        if proc.returncode != 0:
            raise RuntimeError(
                server._with_quota_note(
                    f"agy exited {proc.returncode}: {proc.stderr[-300:]}",
                    f"{proc.stderr}\n{(proc.stdout or '')[-2000:]}",
                    model,
                )
            )
        deadline = time.time() + 5.0
        while True:
            conv = _only_conv(home)
            try:
                if conv:
                    ans = _read_isolated_response(home, conv)
                    return WorkerResult(
                        index,
                        True,
                        answer=ans,
                        elapsed=round(time.time() - t0, 1),
                        workspace=workspace,
                    )
            except RuntimeError:
                pass
            if time.time() >= deadline:
                # agy exited 0 but wrote no readable transcript — its stderr holds
                # the real cause (e.g. a 1.1.3 permission soft-deny), so surface it
                # rather than a bare scrape failure that reads as a bridge bug.
                tail = (proc.stderr or "").strip()
                raise RuntimeError(
                    "no readable transcript after agy exit"
                    + (f" — stderr: {tail[-300:]}" if tail else "")
                )
            time.sleep(0.1)
    except Exception as e:  # error isolation: never propagate, return the failure
        if _looks_like_auth_failure(str(e)):
            # agy went to its sign-in flow because the isolated HOME hides the
            # stored credentials. Latch serialized mode and give this worker its
            # answer rather than reporting a failure the user can do nothing about.
            _mark_isolation_broken(str(e))
            shutil.rmtree(home, ignore_errors=True)
            return _run_text_worker_serialized(index, prompt, workspace, model, timeout_s, t0, plan)
        return WorkerResult(
            index, False, error=str(e), elapsed=round(time.time() - t0, 1), workspace=workspace
        )
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _watched_serialized_note(index: int, start: float) -> None:
    """Tell the viewer why a serialized worker shows no live steps.

    The step feed pumps the worker's ISOLATED transcript, and the fallback has no
    isolated home to pump — the run lives in the user's real brain dir among every
    other conversation. Rather than leave an empty panel that reads as a hung
    worker, say what is happening; the final answer still arrives normally.
    """
    import swarm_watch

    swarm_watch.worker_append(
        index,
        [
            {
                "kind": "narration",
                "text": (
                    "agy could not authenticate in an isolated HOME, so this worker "
                    "runs serialized in the real HOME — live steps are unavailable; "
                    "the answer will appear when it finishes."
                ),
                "t": round(time.time() - start, 1),
            }
        ],
    )


def _run_text_worker_watched_serialized(
    index, prompt, workspace, model, timeout_s, start, plan=False
) -> WorkerResult:
    """Watched worker in serialized mode: real progress state, no live step feed."""
    import swarm_watch

    _watched_serialized_note(index, start)
    res = _run_text_worker_serialized(index, prompt, workspace, model, timeout_s, start, plan)
    if res.ok:
        swarm_watch.worker_finish(index, "done", res.answer or "", time.time() - start)
    else:
        swarm_watch.worker_finish(index, "error", res.error or "", time.time() - start)
    return res


def _run_text_worker_watched(
    index, prompt, workspace, model, timeout_s, plan=False
) -> WorkerResult:
    import server
    import swarm_watch

    start = time.time()
    if not isolation_ok():
        swarm_watch.worker_update(index, status="working", started=start)
        return _run_text_worker_watched_serialized(
            index, prompt, workspace, model, timeout_s, start, plan
        )
    home = _make_isolated_home()
    swarm_watch.worker_update(index, status="working", started=start)
    feed = _Feed(home, index, start)
    try:
        args = server._agy_base_args(timeout_s, plan)
        if model:
            args += ["--model", model]
        args += ["-p", prompt]
        proc = proc_tree.popen(
            args,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_TEXT,
            env=_env_for_home(home),
            **server._spawn_kwargs(),
        )
        # Drain stdout/stderr on threads so a large answer can't fill the pipe buffer
        # and hang agy into a false timeout (see server._drain_pipe); the answer still
        # comes from this worker's isolated transcript.
        out_t, out_chunks = server._drain_pipe(proc.stdout)
        err_t, err_chunks = server._drain_pipe(proc.stderr)
        hard = start + timeout_s + 30
        while proc.poll() is None:
            if time.time() > hard:
                proc_tree.kill_tree(proc)  # the grandchild must die too — see proc_tree
                raise RuntimeError("timeout")
            feed.pump()
            swarm_watch.worker_update(index, elapsed=round(time.time() - start, 1))
            time.sleep(0.4)
        feed.pump()
        out_t.join(timeout=5)
        err_t.join(timeout=5)
        # The timeout first: agy 1.5.0+ exits 3 on it, 1.2.0-1.4.x exited 0.
        if server._print_timed_out("".join(err_chunks)):
            res = _print_timeout_result(
                index, timeout_s, "".join(out_chunks), home, start, workspace
            )
            swarm_watch.worker_finish(index, "error", res.error or "", time.time() - start)
            return res
        if proc.returncode != 0:
            err_text = "".join(err_chunks)
            raise RuntimeError(
                server._with_quota_note(
                    f"agy exited {proc.returncode}: {err_text[-300:]}",
                    f"{err_text}\n{''.join(out_chunks)[-2000:]}",
                    model,
                )
            )
        deadline = time.time() + 5.0
        while True:
            conv = _only_conv(home)
            try:
                if conv:
                    ans = _read_isolated_response(home, conv)
                    swarm_watch.worker_finish(index, "done", ans, time.time() - start)
                    return WorkerResult(
                        index,
                        True,
                        answer=ans,
                        elapsed=round(time.time() - start, 1),
                        workspace=workspace,
                    )
            except RuntimeError:
                pass
            if time.time() >= deadline:
                # Same exit-0-with-no-answer case as the non-watched worker; agy's
                # stderr (already drained into err_chunks) names the real cause.
                tail = "".join(err_chunks).strip()
                raise RuntimeError(
                    "no readable transcript after agy exit"
                    + (f" — stderr: {tail[-300:]}" if tail else "")
                )
            time.sleep(0.1)
    except Exception as e:
        if _looks_like_auth_failure(str(e)):
            _mark_isolation_broken(str(e))
            shutil.rmtree(home, ignore_errors=True)
            return _run_text_worker_watched_serialized(
                index, prompt, workspace, model, timeout_s, start, plan
            )
        swarm_watch.worker_finish(index, "error", str(e), time.time() - start)
        return WorkerResult(
            index, False, error=str(e), elapsed=round(time.time() - start, 1), workspace=workspace
        )
    finally:
        shutil.rmtree(home, ignore_errors=True)


# ----------------------------------------------------------------------------- image swarm
def _finalize_or_timeout(home, target, agy_text, start, stderr: str, timeout_s: int):
    """_finalize_image_isolated, but a turn agy cut at --print-timeout that produced
    no image reports the timeout, not "no image file found".

    An image written before the cut is still returned: the file is checked first.
    agy 1.5.0+ exits 3 on the timeout and 1.2.0-1.4.x exit 0; the stderr mark is the
    same on both, so this does not look at the exit code."""
    import server

    try:
        return _finalize_image_isolated(home, target, agy_text, start)
    except RuntimeError as e:
        if server._print_timed_out(stderr):
            raise RuntimeError(f"{server._print_timeout_error(timeout_s)} ({e})") from e
        raise


def _finalize_image_isolated(home: Path, target: str, agy_text: Optional[str], start: float):
    """Locate the generated image (target / agy-reported path / isolated scratch),
    correct its extension to the real bytes, and return (path, fmt, size).

    Same rules as server._finalize_image, with this worker's own scratch dir: agy's
    reply path counts only inside that scratch dir or the target's folder, a file
    outside the scratch dir is copied rather than moved, and an existing file at the
    final name is never replaced (-1, -2, …)."""
    import server

    scratch = home / ".gemini" / "antigravity-cli" / "scratch"
    src = server._image_source(target, agy_text, start, scratch)
    if src is None:
        raise RuntimeError(f"no image file produced (looked at {target} and scratch)")
    return server._place_image(src, target, scratch)


def _run_image_worker_serialized(
    index, prompt, target, workspace, timeout_s, start
) -> WorkerResult:
    """One image worker WITHOUT HOME isolation, serialized behind _AGY_LOCK.

    Mirrors _run_text_worker_serialized, and finalizes through server._finalize_image
    (the real-HOME twin of _finalize_image_isolated: agy writes to the target path
    or to the REAL scratch dir here, not to a per-worker one).
    """
    import server

    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        wrapped = server._wrap_image_prompt(prompt, target)
        agy_text: Optional[str] = None
        agy_error: Optional[RuntimeError] = None
        try:
            agy_text = server._run_agy(wrapped, workspace, False, timeout_s, pin=False)
        except RuntimeError as e:
            # The image may exist even when the answer read failed; only report this
            # if nothing was produced (same rule as server.antigravity_image), and
            # then name it, so a timeout is not reported as a missing file.
            agy_error = e
        try:
            final_path, fmt, size = server._finalize_image(target, agy_text, start)
        except RuntimeError as fin_err:
            if agy_error is not None:
                raise RuntimeError(f"{fin_err} (agy also failed: {agy_error})") from agy_error
            raise
        return WorkerResult(
            index,
            True,
            answer=final_path,
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            image_path=final_path,
            image_format=fmt,
            image_size=size,
        )
    except Exception as e:  # noqa: BLE001 — error isolation: one worker must not sink the swarm
        return WorkerResult(
            index, False, error=str(e), elapsed=round(time.time() - start, 1), workspace=workspace
        )


def _run_image_worker(index, prompt, target, workspace, timeout_s) -> WorkerResult:
    import server

    if not isolation_ok():
        return _run_image_worker_serialized(
            index, prompt, target, workspace, timeout_s, time.time()
        )
    home = _make_isolated_home()
    start = time.time()
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        wrapped = server._wrap_image_prompt(prompt, target)
        args = server._agy_base_args(timeout_s) + ["-p", wrapped]
        proc = proc_tree.run_captured(
            args,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            env=_env_for_home(home),
            **_TEXT,
            timeout=timeout_s + 30,
            **server._spawn_kwargs(),
        )
        agy_text = None
        if proc.returncode == 0:
            conv = _only_conv(home)
            if conv:
                try:
                    agy_text = _read_isolated_response(home, conv)
                except RuntimeError:
                    pass
        else:
            output = (proc.stderr or "") + (proc.stdout or "")
            if _looks_like_auth_failure(output) or server._looks_like_quota_failure(output):
                # Surface agy's sign-in or quota failure as the error, so the handler
                # below can recognize the first and the caller sees which model
                # family is out for the second. Otherwise this path swallows a
                # non-zero exit and fails with "no image file produced", which hides
                # the real cause.
                raise RuntimeError(
                    server._with_quota_note(
                        f"agy exited {proc.returncode}: {(proc.stderr or '')[-300:]}", output
                    )
                )
        final_path, fmt, size = _finalize_or_timeout(
            home, target, agy_text, start, proc.stderr or "", timeout_s
        )
        return WorkerResult(
            index,
            True,
            answer=final_path,
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            image_path=final_path,
            image_format=fmt,
            image_size=size,
        )
    except Exception as e:
        if _looks_like_auth_failure(str(e)):
            _mark_isolation_broken(str(e))
            shutil.rmtree(home, ignore_errors=True)
            return _run_image_worker_serialized(index, prompt, target, workspace, timeout_s, start)
        return WorkerResult(
            index, False, error=str(e), elapsed=round(time.time() - start, 1), workspace=workspace
        )
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _run_image_worker_watched_serialized(
    index, prompt, target, workspace, timeout_s, start
) -> WorkerResult:
    """Watched image worker in serialized mode: no live step feed, same result."""
    import swarm_watch

    _watched_serialized_note(index, start)
    res = _run_image_worker_serialized(index, prompt, target, workspace, timeout_s, start)
    if res.ok:
        caption = f"Saved to {res.image_path}\nformat={res.image_format} · {res.image_size} bytes"
        swarm_watch.worker_finish(
            index, "done", caption, time.time() - start, image=res.image_path or ""
        )
    else:
        swarm_watch.worker_finish(index, "error", res.error or "", time.time() - start)
    return res


def _run_image_worker_watched(index, prompt, target, workspace, timeout_s) -> WorkerResult:
    import server
    import swarm_watch

    start = time.time()
    if not isolation_ok():
        swarm_watch.worker_update(index, status="working", started=start)
        return _run_image_worker_watched_serialized(
            index, prompt, target, workspace, timeout_s, start
        )
    home = _make_isolated_home()
    swarm_watch.worker_update(index, status="working", started=start)
    feed = _Feed(home, index, start)
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        wrapped = server._wrap_image_prompt(prompt, target)
        args = server._agy_base_args(timeout_s) + ["-p", wrapped]
        proc = proc_tree.popen(
            args,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_TEXT,
            env=_env_for_home(home),
            **server._spawn_kwargs(),
        )
        # Drain both pipes so agy can't hang on a full pipe buffer (see
        # server._drain_pipe); the image answer comes from the transcript/scratch.
        out_t, _out = server._drain_pipe(proc.stdout)
        err_t, err_chunks = server._drain_pipe(proc.stderr)
        hard = start + timeout_s + 30
        while proc.poll() is None:
            if time.time() > hard:
                proc_tree.kill_tree(proc)  # the grandchild must die too — see proc_tree
                raise RuntimeError("timeout")
            feed.pump()
            swarm_watch.worker_update(index, elapsed=round(time.time() - start, 1))
            time.sleep(0.4)
        feed.pump()
        out_t.join(timeout=5)
        err_t.join(timeout=5)
        agy_text = None
        conv = _only_conv(home)
        if conv:
            try:
                agy_text = _read_isolated_response(home, conv)
            except RuntimeError:
                pass
        err_text = "".join(err_chunks)
        if proc.returncode != 0 and (
            _looks_like_auth_failure(err_text) or server._looks_like_quota_failure(err_text)
        ):
            # See the non-watched twin: surface the sign-in or quota failure instead
            # of letting it become a misleading "no image file produced".
            message = f"agy exited {proc.returncode}: {err_text[-300:]}"
            raise RuntimeError(server._with_quota_note(message, err_text))
        final_path, fmt, size = _finalize_or_timeout(
            home, target, agy_text, start, err_text, timeout_s
        )
        caption = f"Saved to {final_path}\nformat={fmt} · {size} bytes"
        swarm_watch.worker_finish(index, "done", caption, time.time() - start, image=final_path)
        return WorkerResult(
            index,
            True,
            answer=final_path,
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            image_path=final_path,
            image_format=fmt,
            image_size=size,
        )
    except Exception as e:
        if _looks_like_auth_failure(str(e)):
            _mark_isolation_broken(str(e))
            shutil.rmtree(home, ignore_errors=True)
            return _run_image_worker_watched_serialized(
                index, prompt, target, workspace, timeout_s, start
            )
        swarm_watch.worker_finish(index, "error", str(e), time.time() - start)
        return WorkerResult(
            index, False, error=str(e), elapsed=round(time.time() - start, 1), workspace=workspace
        )
    finally:
        shutil.rmtree(home, ignore_errors=True)


def swarm_image(
    prompts: list[str],
    output_paths: list[str],
    workspaces: Union[None, str, list] = None,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    timeout_s: int = 240,
    watch: bool = False,
    on_progress: Optional[ProgressCallback] = None,
) -> list[WorkerResult]:
    """Generate N images in parallel (one isolated agy worker each).

    `on_progress(done, total)` is called from this thread: once with done=0, then after
    each finished worker (see _notify)."""
    if len(output_paths) != len(prompts):
        raise ValueError("output_paths must align with prompts")
    ws = _normalize_workspaces(len(prompts), workspaces)
    import server

    # Each worker gets a name no existing file and no other worker holds, so two
    # images never land on the same path and none replaces a file already there.
    targets: list[str] = []
    for p in output_paths:
        targets.append(server._free_path(os.path.abspath(p), frozenset(targets)))
    runner = _run_image_worker_watched if watch else _run_image_worker
    if watch:
        import swarm_watch

        swarm_watch.init(
            _labels(prompts),
            _repos(ws),
            time.time(),
            prompts,
            timeout_s,
            models=_watch_models([{"backend": "antigravity"}] * len(prompts)),
        )
        swarm_watch.open_window(len(prompts))
    results: list[Optional[WorkerResult]] = [None] * len(prompts)
    _notify(on_progress, 0, len(prompts))
    with ThreadPoolExecutor(max_workers=max(1, max_concurrency)) as ex:
        futs = [
            _submit_in_context(ex, runner, i, prompts[i], targets[i], ws[i], timeout_s)
            for i in range(len(prompts))
        ]
        for done, fut in enumerate(as_completed(futs), start=1):
            r = fut.result()
            results[r.index] = r
            _notify(on_progress, done, len(prompts))
    return [r for r in results if r is not None]


# ----------------------------------------------------------------------------- formatting
def format_image_results(results: list[WorkerResult]) -> str:
    """Render image-swarm results as one readable block for the MCP host."""
    parts = []
    for r in sorted(results, key=lambda r: r.index):
        if r.ok:
            parts.append(
                f"[image {r.index}] OK ({r.elapsed}s) {r.image_format} "
                f"{r.image_size} bytes -> {r.image_path}"
            )
        else:
            parts.append(f"[image {r.index}] ERROR ({r.elapsed}s) {r.error}")
    ok = sum(1 for r in results if r.ok)
    return f"image swarm: {ok}/{len(results)} succeeded\n\n" + "\n".join(parts)


# ----------------------------------------------------------------- unified agent swarm
_BACKEND_ALIASES = {
    "antigravity": "antigravity",
    "agy": "antigravity",
    "gemini": "antigravity",
    "codex": "codex",
    "openai": "codex",
    "copilot": "copilot",
    "github": "copilot",
    "gh": "copilot",
    "cursor": "cursor",
    "claude": "claude",
    "claude-code": "claude",
    "anthropic": "claude",
    "grok": "grok",
    "grok-build": "grok",
    "xai": "grok",
    "opencode": "opencode",
    "oc": "opencode",
    "sst": "opencode",
    "muse": "muse",
    "muse-code": "muse",
    "meta": "muse",
}

# Backends the swarm can dispatch to, in the order the error message lists them.
_BACKEND_NAMES = (
    "antigravity",
    "codex",
    "copilot",
    "cursor",
    "claude",
    "grok",
    "opencode",
    "muse",
)


def _normalize_tasks(tasks) -> list[dict]:
    """Validate + canonicalize agent_swarm tasks into a uniform list.

    Each task is {backend, prompt, workspace?, sandbox?, model?, effort?}. backend
    accepts a few aliases; `effort` is for codex and claude workers only (any other
    backend refuses it); `model` applies to every backend (agy's --model works in print
    mode as of 1.0.16). `sandbox` applies to every backend too, now that Antigravity has
    plan mode to honour "read-only" with — it used to be silently ignored there, so
    an agy task could ask to be fenced and run unrestricted anyway. What each policy
    means still differs per backend: Codex's is an enforced OS sandbox, Grok's is on
    Linux/macOS only, and Copilot's, Cursor's, opencode's and Antigravity's are
    agent-level. See server.plan_from_sandbox for which policies agy can and cannot
    honour.

    Raises ValueError naming the offending index on bad input.
    """
    if not isinstance(tasks, list):
        raise ValueError("tasks must be a list of {backend, prompt, ...} objects")
    out: list[dict] = []
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            raise ValueError(f"task {i}: must be an object, got {type(t).__name__}")
        backend = _BACKEND_ALIASES.get(str(t.get("backend", "")).strip().lower())
        if backend is None:
            allowed = ", ".join(repr(b) for b in _BACKEND_NAMES)
            raise ValueError(
                f"task {i}: backend must be one of {allowed} (got {t.get('backend')!r})"
            )
        prompt = t.get("prompt")
        if not prompt or not str(prompt).strip():
            raise ValueError(f"task {i}: 'prompt' is required")
        ws = t.get("workspace")
        try:
            workspace = proc_tree.resolve_workspace(ws)
        except ValueError as e:
            raise ValueError(f"task {i}: {e}") from e
        sandbox = None
        model = None
        effort = None  # codex and claude only; any other backend refuses it below
        plan = False  # antigravity only; see the else-branch below
        if backend == "codex":
            import codex_bridge

            sandbox = t.get("sandbox") or codex_bridge.DEFAULT_SANDBOX
            codex_bridge.validate_sandbox(sandbox)  # fail fast on a bad policy
            model = t.get("model") or None
            effort = codex_bridge.validate_effort(t.get("effort"))
        elif backend == "copilot":
            import copilot_bridge

            sandbox = t.get("sandbox") or copilot_bridge.DEFAULT_SANDBOX
            copilot_bridge.validate_sandbox(sandbox)  # fail fast on a bad policy
            model = t.get("model") or None
        elif backend == "cursor":
            import cursor_bridge

            sandbox = t.get("sandbox") or cursor_bridge.DEFAULT_SANDBOX
            cursor_bridge.validate_sandbox(sandbox)  # fail fast on a bad policy
            model = cursor_bridge.validate_model(t.get("model") or None)  # fail fast on a typo
        elif backend == "claude":
            import claude_bridge

            sandbox = t.get("sandbox") or claude_bridge.DEFAULT_SANDBOX
            claude_bridge.validate_sandbox(sandbox)  # fail fast on a bad policy
            model = claude_bridge.validate_model(t.get("model") or None)
            effort = claude_bridge.validate_effort(t.get("effort"))
        elif backend == "grok":
            import grok_bridge

            sandbox = t.get("sandbox") or grok_bridge.DEFAULT_SANDBOX
            grok_bridge.validate_sandbox(sandbox)  # fail fast on a bad policy
            model = grok_bridge.validate_model(t.get("model") or None)  # fail fast on a typo
        elif backend == "opencode":
            import opencode_bridge

            sandbox = t.get("sandbox") or opencode_bridge.DEFAULT_SANDBOX
            opencode_bridge.validate_sandbox(sandbox)  # fail fast on a bad policy
            model = opencode_bridge.validate_model(t.get("model") or None)  # fail fast on a typo
        elif backend == "muse":
            import muse_bridge

            sandbox = t.get("sandbox") or muse_bridge.DEFAULT_SANDBOX
            muse_bridge.validate_sandbox(sandbox)  # fail fast on a bad policy
            model = muse_bridge.validate_model(t.get("model") or None)  # lenient: any id
        else:  # antigravity
            import server

            model = server.validate_model(t.get("model") or None)  # fail fast on a typo
            # Omitted: unrestricted, unless AGENT_INTERN_AGY_DEFAULT=plan; see below.
            sandbox = t.get("sandbox") or None
            try:
                if sandbox is None:
                    plan = server.agy_plan_by_default()
                else:
                    plan = server.plan_from_sandbox(sandbox)
                if plan:
                    # Fail the whole swarm here rather than N calls in: this checks
                    # the agy version gate and the slash-command guard that plan mode
                    # needs in place of --disable-slash-commands.
                    server._check_plan_mode(str(prompt))
            except (ValueError, proc_tree.CliNotFound) as e:  # CliNotFound: no agy at all
                raise ValueError(f"task {i}: {e}") from e
        if backend not in ("codex", "claude") and str(t.get("effort") or "").strip():
            # Refuse rather than ignore: a worker that quietly ran at another level
            # would answer a question the caller did not ask.
            hint = (
                " (agy takes the level in the model name instead, e.g. gemini-4-argon-low;"
                " its --effort conflicts with a model that already names one)"
                if backend == "antigravity"
                else ""
            )
            raise ValueError(
                f"task {i}: 'effort' is only supported for the codex and claude backends, "
                f"not {backend!r}{hint}"
            )
        out.append(
            {
                "backend": backend,
                "prompt": str(prompt),
                "workspace": workspace,
                "sandbox": sandbox,
                "model": model,
                "effort": effort,
                "plan": plan,
            }
        )
    return out


def _run_codex_worker(
    index, prompt, workspace, sandbox, model, timeout_s, effort=None
) -> WorkerResult:
    import codex_bridge

    start = time.time()
    try:
        ans = codex_bridge.run_codex(
            prompt, workspace, sandbox, model, False, timeout_s, pin=False, effort=effort
        )
        return WorkerResult(
            index,
            True,
            answer=ans,
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            backend="codex",
        )
    except Exception as e:  # noqa: BLE001 — error isolation: one worker must not sink the swarm
        return WorkerResult(
            index,
            False,
            error=str(e),
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            backend="codex",
        )


def _run_codex_worker_watched(
    index, prompt, workspace, sandbox, model, timeout_s, effort=None
) -> WorkerResult:
    import codex_bridge
    import server
    import swarm_watch

    start = time.time()
    swarm_watch.worker_update(index, status="working", started=start)

    def on_event(ev: dict) -> None:
        lines = server._codex_event_to_watch_lines(ev)
        if lines:
            t = round(time.time() - start, 1)
            swarm_watch.worker_append(index, [{"kind": k, "text": x, "t": t} for k, x in lines])

    try:
        ans = codex_bridge.run_codex_streaming(
            prompt, workspace, sandbox, model, False, timeout_s, on_event, pin=False, effort=effort
        )
        swarm_watch.worker_finish(index, "done", ans, time.time() - start)
        return WorkerResult(
            index,
            True,
            answer=ans,
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            backend="codex",
        )
    except Exception as e:  # noqa: BLE001
        swarm_watch.worker_finish(index, "error", str(e), time.time() - start)
        return WorkerResult(
            index,
            False,
            error=str(e),
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            backend="codex",
        )


def _run_copilot_worker(index, prompt, workspace, sandbox, model, timeout_s) -> WorkerResult:
    import copilot_bridge

    start = time.time()
    try:
        ans = copilot_bridge.run_copilot(
            prompt, workspace, sandbox, model, False, timeout_s, pin=False
        )
        return WorkerResult(
            index,
            True,
            answer=ans,
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            backend="copilot",
        )
    except Exception as e:  # noqa: BLE001 — error isolation: one worker must not sink the swarm
        return WorkerResult(
            index,
            False,
            error=str(e),
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            backend="copilot",
        )


def _run_copilot_worker_watched(
    index, prompt, workspace, sandbox, model, timeout_s
) -> WorkerResult:
    import copilot_bridge
    import server
    import swarm_watch

    start = time.time()
    swarm_watch.worker_update(index, status="working", started=start)

    def on_event(ev: dict) -> None:
        lines = server._copilot_event_to_watch_lines(ev)
        if lines:
            t = round(time.time() - start, 1)
            swarm_watch.worker_append(index, [{"kind": k, "text": x, "t": t} for k, x in lines])

    try:
        ans = copilot_bridge.run_copilot_streaming(
            prompt, workspace, sandbox, model, False, timeout_s, on_event, pin=False
        )
        swarm_watch.worker_finish(index, "done", ans, time.time() - start)
        return WorkerResult(
            index,
            True,
            answer=ans,
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            backend="copilot",
        )
    except Exception as e:  # noqa: BLE001
        swarm_watch.worker_finish(index, "error", str(e), time.time() - start)
        return WorkerResult(
            index,
            False,
            error=str(e),
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            backend="copilot",
        )


def _run_cursor_worker(index, prompt, workspace, sandbox, model, timeout_s) -> WorkerResult:
    import cursor_bridge

    start = time.time()
    try:
        ans = cursor_bridge.run_cursor(
            prompt, workspace, sandbox, model, False, timeout_s, pin=False
        )
        return WorkerResult(
            index,
            True,
            answer=ans,
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            backend="cursor",
        )
    except Exception as e:  # noqa: BLE001 — error isolation: one worker must not sink the swarm
        return WorkerResult(
            index,
            False,
            error=str(e),
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            backend="cursor",
        )


def _run_cursor_worker_watched(index, prompt, workspace, sandbox, model, timeout_s) -> WorkerResult:
    import cursor_bridge
    import server
    import swarm_watch

    start = time.time()
    swarm_watch.worker_update(index, status="working", started=start)

    def on_event(ev: dict) -> None:
        lines = server._cursor_event_to_watch_lines(ev)
        if lines:
            t = round(time.time() - start, 1)
            swarm_watch.worker_append(index, [{"kind": k, "text": x, "t": t} for k, x in lines])

    try:
        ans = cursor_bridge.run_cursor_streaming(
            prompt, workspace, sandbox, model, False, timeout_s, on_event, pin=False
        )
        swarm_watch.worker_finish(index, "done", ans, time.time() - start)
        return WorkerResult(
            index,
            True,
            answer=ans,
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            backend="cursor",
        )
    except Exception as e:  # noqa: BLE001
        swarm_watch.worker_finish(index, "error", str(e), time.time() - start)
        return WorkerResult(
            index,
            False,
            error=str(e),
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            backend="cursor",
        )


def _run_claude_worker(
    index, prompt, workspace, sandbox, model, timeout_s, effort=None
) -> WorkerResult:
    import claude_bridge

    start = time.time()
    try:
        ans = claude_bridge.run_claude(
            prompt, workspace, sandbox, model, False, timeout_s, pin=False, effort=effort
        )
        return WorkerResult(
            index,
            True,
            answer=ans,
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            backend="claude",
        )
    except Exception as e:  # noqa: BLE001 — error isolation: one worker must not sink the swarm
        return WorkerResult(
            index,
            False,
            error=str(e),
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            backend="claude",
        )


def _run_claude_worker_watched(
    index, prompt, workspace, sandbox, model, timeout_s, effort=None
) -> WorkerResult:
    import claude_bridge
    import server
    import swarm_watch

    start = time.time()
    swarm_watch.worker_update(index, status="working", started=start)

    def on_event(ev: dict) -> None:
        lines = server._claude_event_to_watch_lines(ev)
        if lines:
            t = round(time.time() - start, 1)
            swarm_watch.worker_append(index, [{"kind": k, "text": x, "t": t} for k, x in lines])

    try:
        ans = claude_bridge.run_claude_streaming(
            prompt, workspace, sandbox, model, False, timeout_s, on_event, pin=False, effort=effort
        )
        swarm_watch.worker_finish(index, "done", ans, time.time() - start)
        return WorkerResult(
            index,
            True,
            answer=ans,
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            backend="claude",
        )
    except Exception as e:  # noqa: BLE001
        swarm_watch.worker_finish(index, "error", str(e), time.time() - start)
        return WorkerResult(
            index,
            False,
            error=str(e),
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            backend="claude",
        )


def _run_grok_worker(index, prompt, workspace, sandbox, model, timeout_s) -> WorkerResult:
    # NOTE: no HOME/GROK_HOME isolation here, unlike the agy workers. Grok mints a
    # fresh session per headless run, so parallel runs don't collide (verified:
    # concurrent `grok -p` calls don't deadlock on ~/.grok's lock files) — and
    # isolating GROK_HOME would HIDE the user's auth.json, since grok caches
    # credentials inside that same dir. That's issue #2's failure mode, on every
    # platform rather than just macOS.
    import grok_bridge

    start = time.time()
    try:
        ans = grok_bridge.run_grok(prompt, workspace, sandbox, model, False, timeout_s, pin=False)
        return WorkerResult(
            index,
            True,
            answer=ans,
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            backend="grok",
        )
    except Exception as e:  # noqa: BLE001 — error isolation: one worker must not sink the swarm
        return WorkerResult(
            index,
            False,
            error=str(e),
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            backend="grok",
        )


def _run_grok_worker_watched(index, prompt, workspace, sandbox, model, timeout_s) -> WorkerResult:
    import grok_bridge
    import server
    import swarm_watch

    start = time.time()
    swarm_watch.worker_update(index, status="working", started=start)

    def on_event(ev: dict) -> None:
        lines = server._grok_event_to_watch_lines(ev)
        if lines:
            t = round(time.time() - start, 1)
            swarm_watch.worker_append(index, [{"kind": k, "text": x, "t": t} for k, x in lines])

    try:
        ans = grok_bridge.run_grok_streaming(
            prompt, workspace, sandbox, model, False, timeout_s, on_event, pin=False
        )
        swarm_watch.worker_finish(index, "done", ans, time.time() - start)
        return WorkerResult(
            index,
            True,
            answer=ans,
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            backend="grok",
        )
    except Exception as e:  # noqa: BLE001
        swarm_watch.worker_finish(index, "error", str(e), time.time() - start)
        return WorkerResult(
            index,
            False,
            error=str(e),
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            backend="grok",
        )


def _opencode_timeout(timeout_s: int) -> int:
    """The swarm's per-worker budget, raised to opencode's own default if lower.

    The swarm shares one `timeout_s` across every backend and defaults it to 180s,
    and opencode's own is a 300s ceiling: its free tier answers in seconds today
    (2.5-6.5 s on 1.19.5) but has been queue-scheduled to 152-428 s before (1.18.x),
    when a 180s budget killed about half the workers mid-answer. Raising (never
    lowering) to the same ceiling the opencode_* tools use keeps a mixed swarm
    honest; a worker that finishes early never notices the larger budget.
    """
    import opencode_bridge

    return max(timeout_s, opencode_bridge.DEFAULT_TIMEOUT_S)


def _run_opencode_worker(index, prompt, workspace, sandbox, model, timeout_s) -> WorkerResult:
    # NOTE: no HOME/XDG isolation here, for the same reason as the grok workers.
    # opencode mints a fresh session per headless run, and its credentials live
    # under the same data dir a relocation would hide (issue #2's failure mode).
    import opencode_bridge

    timeout_s = _opencode_timeout(timeout_s)
    start = time.time()
    try:
        ans = opencode_bridge.run_opencode(
            prompt, workspace, sandbox, model, False, timeout_s, pin=False
        )
        return WorkerResult(
            index,
            True,
            answer=ans,
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            backend="opencode",
        )
    except Exception as e:  # noqa: BLE001 — error isolation: one worker must not sink the swarm
        return WorkerResult(
            index,
            False,
            error=str(e),
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            backend="opencode",
        )


def _run_opencode_worker_watched(
    index, prompt, workspace, sandbox, model, timeout_s
) -> WorkerResult:
    import opencode_bridge
    import server
    import swarm_watch

    timeout_s = _opencode_timeout(timeout_s)  # see _opencode_timeout
    start = time.time()
    # The raised budget, not the swarm's: the row's time bar is drawn against it.
    swarm_watch.worker_update(index, status="working", started=start, timeout=timeout_s)

    def on_event(ev: dict) -> None:
        lines = server._opencode_event_to_watch_lines(ev)
        if lines:
            t = round(time.time() - start, 1)
            swarm_watch.worker_append(index, [{"kind": k, "text": x, "t": t} for k, x in lines])

    try:
        ans = opencode_bridge.run_opencode_streaming(
            prompt, workspace, sandbox, model, False, timeout_s, on_event, pin=False
        )
        swarm_watch.worker_finish(index, "done", ans, time.time() - start)
        return WorkerResult(
            index,
            True,
            answer=ans,
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            backend="opencode",
        )
    except Exception as e:  # noqa: BLE001
        swarm_watch.worker_finish(index, "error", str(e), time.time() - start)
        return WorkerResult(
            index,
            False,
            error=str(e),
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            backend="opencode",
        )


def _run_muse_worker(index, prompt, workspace, sandbox, model, timeout_s) -> WorkerResult:
    # NOTE: no HOME/XDG isolation, for the same reason as the grok workers: muse's
    # login lives in ~/.config/muse/auth.json, which a relocated home would hide.
    # Isolation isn't needed either: each worker gets a fresh muse-minted session
    # (pin=False), and four concurrent `muse exec` runs were verified not to contend.
    import muse_bridge

    start = time.time()
    try:
        ans = muse_bridge.run_muse(prompt, workspace, sandbox, model, False, timeout_s, pin=False)
        return WorkerResult(
            index,
            True,
            answer=ans,
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            backend="muse",
        )
    except Exception as e:  # noqa: BLE001 — error isolation: one worker must not sink the swarm
        return WorkerResult(
            index,
            False,
            error=str(e),
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            backend="muse",
        )


def _run_muse_worker_watched(index, prompt, workspace, sandbox, model, timeout_s) -> WorkerResult:
    import muse_bridge
    import swarm_watch

    start = time.time()
    swarm_watch.worker_update(index, status="working", started=start)
    to_lines = muse_bridge.watch_mapper()

    def on_event(ev: dict) -> None:
        lines = to_lines(ev)
        if lines:
            t = round(time.time() - start, 1)
            swarm_watch.worker_append(index, [{"kind": k, "text": x, "t": t} for k, x in lines])

    try:
        ans = muse_bridge.run_muse_streaming(
            prompt, workspace, sandbox, model, False, timeout_s, on_event, pin=False
        )
        swarm_watch.worker_finish(index, "done", ans, time.time() - start)
        return WorkerResult(
            index,
            True,
            answer=ans,
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            backend="muse",
        )
    except Exception as e:  # noqa: BLE001
        swarm_watch.worker_finish(index, "error", str(e), time.time() - start)
        return WorkerResult(
            index,
            False,
            error=str(e),
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            backend="muse",
        )


def swarm_agents(
    tasks,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    timeout_s: int = 180,
    watch: bool = False,
    labels: Optional[list[str]] = None,
    on_progress: Optional[ProgressCallback] = None,
) -> list[WorkerResult]:
    """Run a heterogeneous swarm: each task dispatches to its named backend's worker,
    all concurrently in one pool (and, with watch, one shared dashboard).

    `labels` are the dashboard's card captions, one per task; by default each is the
    first line of its prompt. A preset passes its members' roles instead, because
    all of its prompts open with the same boilerplate.

    `on_progress(done, total)` is called from this thread: once with done=0 when the
    tasks are validated, then after each finished worker (failed ones count).
    """
    norm = _normalize_tasks(tasks)
    n = len(norm)
    if n == 0:
        return []
    prompts = [t["prompt"] for t in norm]
    workspaces = [t["workspace"] for t in norm]
    backends = [t["backend"] for t in norm]
    if watch:
        import swarm_watch

        swarm_watch.init(
            labels if labels and len(labels) == n else _labels(prompts),
            _repos(workspaces),
            time.time(),
            prompts,
            timeout_s,
            backends=backends,
            models=_watch_models(norm),
        )
        swarm_watch.open_window(n)

    import server

    def runner(i: int) -> WorkerResult:
        t = norm[i]
        if t["backend"] not in server.ENABLED_BACKENDS:
            # Fails in place like a backend that isn't installed, so a preset with
            # one member turned off still runs the rest.
            if t["backend"] == "claude" and server.CLAUDE_HIDDEN:
                error = f"backend 'claude' is off: {server.CLAUDE_HIDDEN_NOTE}"
            else:
                error = (
                    f"backend {t['backend']!r} is turned off by AGENT_INTERN_BACKENDS"
                    f" (on: {', '.join(server.ENABLED_BACKENDS)})"
                )
            if watch:
                import swarm_watch

                swarm_watch.worker_finish(i, "error", error, 0.0)
            return WorkerResult(i, False, error=error, workspace=t["workspace"])
        if t["backend"] == "codex":
            fn = _run_codex_worker_watched if watch else _run_codex_worker
            return fn(
                i, t["prompt"], t["workspace"], t["sandbox"], t["model"], timeout_s, t.get("effort")
            )
        if t["backend"] == "copilot":
            fn = _run_copilot_worker_watched if watch else _run_copilot_worker
            return fn(i, t["prompt"], t["workspace"], t["sandbox"], t["model"], timeout_s)
        if t["backend"] == "cursor":
            fn = _run_cursor_worker_watched if watch else _run_cursor_worker
            return fn(i, t["prompt"], t["workspace"], t["sandbox"], t["model"], timeout_s)
        if t["backend"] == "claude":
            fn = _run_claude_worker_watched if watch else _run_claude_worker
            return fn(
                i, t["prompt"], t["workspace"], t["sandbox"], t["model"], timeout_s, t.get("effort")
            )
        if t["backend"] == "grok":
            fn = _run_grok_worker_watched if watch else _run_grok_worker
            return fn(i, t["prompt"], t["workspace"], t["sandbox"], t["model"], timeout_s)
        if t["backend"] == "opencode":
            fn = _run_opencode_worker_watched if watch else _run_opencode_worker
            return fn(i, t["prompt"], t["workspace"], t["sandbox"], t["model"], timeout_s)
        if t["backend"] == "muse":
            fn = _run_muse_worker_watched if watch else _run_muse_worker
            return fn(i, t["prompt"], t["workspace"], t["sandbox"], t["model"], timeout_s)
        fn = _run_text_worker_watched if watch else _run_text_worker
        return fn(i, t["prompt"], t["workspace"], t["model"], timeout_s, t["plan"])

    results: list[Optional[WorkerResult]] = [None] * n
    _notify(on_progress, 0, n)
    with ThreadPoolExecutor(max_workers=max(1, max_concurrency)) as ex:
        futs = [_submit_in_context(ex, runner, i) for i in range(n)]
        for done, fut in enumerate(as_completed(futs), start=1):
            r = fut.result()
            r.backend = backends[r.index]  # authoritative, even if a runner left it blank
            results[r.index] = r
            _notify(on_progress, done, n)
    return [r for r in results if r is not None]


# ------------------------------------------------------------------- output budget
# Claude Code saves an MCP text result to a file once it is over 50,000 characters (or
# MAX_MCP_OUTPUT_TOKENS, 25,000 tokens by default) and hands the model only a path, and
# its transcript viewer warns above 10,000 tokens. The reply therefore stays under
# 40,000 characters and no worker takes more than 15,000, so a swarm of verbose workers
# arrives inline instead of being rerouted by the host. Antigravity's own limit is
# unknown. What does not fit is written in full under ~/.agent-intern/runs/, never in
# the user's repo, and the reply carries an excerpt and the path.
WORKER_OUTPUT_CAP = 15_000
TOTAL_OUTPUT_CAP = 40_000
EXCERPT_CHARS = 2_000  # head and tail of a spilled answer left in the reply
MIN_EXCERPT_CHARS = 300  # the floor when even the excerpts do not fit
_ENTRY_OVERHEAD = 200  # a worker's header line and separators
_SPILL_NOTE = 400  # the omitted-characters marker and the path line
RUNS_KEEP_DAYS = 14
_RUN_DIR_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")


def runs_root() -> Path:
    return Path.home() / ".agent-intern" / "runs"


def _prune_old_runs(root: Path) -> None:
    """Drop run directories this module made more than RUNS_KEEP_DAYS ago. Best effort;
    only entries named like a run id are touched."""
    cutoff = time.time() - RUNS_KEEP_DAYS * 86400
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    for d in entries:
        try:
            if _RUN_DIR_RE.match(d.name) and d.is_dir() and not d.is_symlink():
                if d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
        except OSError:
            continue


def _new_run_dir() -> Path:
    root = runs_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _prune_old_runs(root)
    d = root / f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    d.mkdir(mode=0o700)
    return d


def _excerpt(text: str, n: int) -> str:
    """The first two thirds and the last third of an `n`-character budget: an answer's
    conclusion is often at its end."""
    head = n * 2 // 3
    tail = n - head
    omitted = len(text) - head - tail
    return f"{text[:head].rstrip()}\n[… {omitted:,} characters omitted …]\n{text[-tail:].lstrip()}"


def budget_results(
    results: list[WorkerResult],
    worker_cap: int = WORKER_OUTPUT_CAP,
    total_cap: int = TOTAL_OUTPUT_CAP,
) -> list[WorkerResult]:
    """Copies of `results` whose answers and errors fit the output budget.

    A text over `worker_cap` is spilled; if the reply would still pass `total_cap`, the
    longest remaining texts are spilled too, and if even excerpts of everything do not
    fit, the excerpts shrink. A spilled text is written in full to
    runs_root()/<run-id>/worker-N.md and replaced by an excerpt and that path (or by
    the excerpt and the reason, if the file cannot be written). Nothing is written
    when everything fits."""
    texts = {r.index: ((r.answer or "") if r.ok else (r.error or "")) for r in results}
    spill = {i for i, t in texts.items() if len(t) > worker_cap}
    excerpt_n = EXCERPT_CHARS

    def total() -> int:
        return sum(
            _ENTRY_OVERHEAD + (excerpt_n + _SPILL_NOTE if i in spill else len(t))
            for i, t in texts.items()
        )

    while total() > total_cap:
        rest = [i for i, t in texts.items() if i not in spill and len(t) > excerpt_n + _SPILL_NOTE]
        if not rest:
            break
        spill.add(max(rest, key=lambda i: len(texts[i])))
    if spill and total() > total_cap:
        excerpt_n = max(MIN_EXCERPT_CHARS, total_cap // len(texts) - _ENTRY_OVERHEAD - _SPILL_NOTE)
    if not spill:
        return results

    run_dir: Optional[Path] = None
    run_dir_error = ""
    out = []
    for r in results:
        if r.index not in spill:
            out.append(r)
            continue
        text = texts[r.index]
        note = ""
        if not run_dir_error:
            try:
                run_dir = run_dir or _new_run_dir()
                stats.note_run_dir(run_dir)
                path = run_dir / f"worker-{r.index}.md"
                state = "OK" if r.ok else "ERROR"
                head = f"# worker {r.index} · {r.backend or '?'} · {state} ({r.elapsed}s)"
                path.write_text(f"{head}\n\n{text}\n", encoding="utf-8")
                try:
                    path.chmod(0o600)
                except OSError:
                    pass
                note = f"\n(full text, {len(text):,} characters: {path})"
            except OSError as e:
                run_dir_error = str(e)
        if run_dir_error:
            note = f"\n(the full text could not be saved: {run_dir_error})"
        shown = _excerpt(text, excerpt_n) + note
        out.append(replace(r, answer=shown) if r.ok else replace(r, error=shown))
    return out


# --------------------------------------------------------------------- MCP progress
_PROGRESS_HEARTBEAT_S = 5.0  # a quiet swarm still reports this often
_PROGRESS_FRACTION_S = 30.0  # how fast the part of a worker "in flight" creeps up


async def run_with_worker_progress(
    ctx: Any, run: Callable[[Optional[ProgressCallback]], str]
) -> str:
    """Run the blocking swarm `run(on_progress)` off the event loop and send MCP progress
    while it works: progress is the number of finished workers over the total ("3/8
    workers done"), which is real, unlike a single call's elapsed-time bar.

    The spec wants progress to increase with every notification, so a swarm that is
    quiet for a while still sends a heartbeat: the finished count plus a fraction below
    one that grows with the time since the last worker finished. With `ctx` None (a
    direct or test call, or a client that sent no progressToken) it is a plain threaded
    call with no notifications. Reporting is best-effort and never fails the run.

    A cancelled call (the client's notifications/cancelled) kills every agent process
    tree the swarm started, as server._run_with_progress does for a single call: the
    call is made proc_tree.CURRENT_CALL before the thread starts, and the swarm's pool
    threads join that context (see _submit_in_context), so proc_tree.popen files each
    worker's process under it. Workers still queued when it is cancelled fail at once
    instead of starting."""
    call = proc_tree.Call()
    token = proc_tree.CURRENT_CALL.set(call)
    try:
        loop = asyncio.get_running_loop()
        wake = asyncio.Event()
        latest: list[Optional[tuple[int, int, float]]] = [None]  # set from the worker thread

        def on_progress(done: int, total: int) -> None:
            latest[0] = (done, total, time.monotonic())
            loop.call_soon_threadsafe(wake.set)

        # asyncio.to_thread copies the context, so the worker thread sees `call`
        task = asyncio.ensure_future(asyncio.to_thread(run, on_progress if ctx else None))
    finally:
        proc_tree.CURRENT_CALL.reset(token)
    try:
        if ctx is None:
            return await task
        return await _report_until_done(ctx, task, latest, wake)
    except asyncio.CancelledError:
        # Off the event loop (wait=False): taskkill can take seconds.
        proc_tree.cancel_call(call, wait=False)
        raise


async def _report_until_done(
    ctx: Any,
    task: "asyncio.Future[str]",
    latest: list,
    wake: asyncio.Event,
) -> str:
    """Send the finished/total progress until `task` is done; return its result."""
    started = time.monotonic()
    last = -1.0
    while not task.done():
        waiter = asyncio.ensure_future(wake.wait())
        try:
            await asyncio.wait(
                {task, waiter}, timeout=_PROGRESS_HEARTBEAT_S, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            waiter.cancel()
        wake.clear()
        snap = latest[0]
        if snap is None:
            continue
        done, total, since = snap
        age = time.monotonic() - since
        progress = float(total) if done >= total else done + age / (age + _PROGRESS_FRACTION_S)
        if progress <= last:
            continue
        last = progress
        try:
            await ctx.report_progress(
                progress=progress,
                total=float(total),
                message=f"{done}/{total} workers done ({int(time.monotonic() - started)}s)",
            )
        except Exception:  # noqa: BLE001 - progress is cosmetic; never break the run
            pass
    return await task  # re-raises any error from the worker thread


def format_agent_results(results: list[WorkerResult]) -> str:
    """Render unified-swarm results as one block, tagged by backend per worker. Long
    answers are cut down to an excerpt and a file path (see budget_results)."""
    results = budget_results(results)
    parts = []
    for r in sorted(results, key=lambda r: r.index):
        head = f"[worker {r.index} · {r.backend or '?'}] {'OK' if r.ok else 'ERROR'} ({r.elapsed}s)"
        if r.workspace:
            head += f" @ {_basename_any(r.workspace)}"
        body = r.answer if r.ok else f"(failed) {r.error}"
        parts.append(f"{head}\n{body}")
    ok = sum(1 for r in results if r.ok)
    stats.note_swarm(len(results), ok)  # usage statistics: worker counts, never text
    return f"agent swarm: {ok}/{len(results)} succeeded\n\n" + "\n\n".join(parts)
