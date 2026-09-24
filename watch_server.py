"""The single-run watch viewer: run state, the localhost HTTP server, the window.

Every watched run (any backend) records its steps here, keyed by run id, and a
small page served on 127.0.0.1 polls them (see watch_ui for the page itself).
_StreamWatch and _WatchFeed turn agy's output into those steps; the other
backends' tool modules feed them directly. swarm_watch is the swarm's
counterpart and shares this module's access check.

This was a section of server.py. server re-imports every name here, so code and
tests written against server.<name> keep working. One difference: code in this
module calls its own names directly, so a test must patch a name HERE (e.g.
watch_server._chromium_app_browsers) to change what this module sees.
"""

import hmac
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

import proc_tree
import watch_ui

# Live "watch" viewer state, served over a localhost HTTP server to a browser page.
# Keyed by a run id so CONCURRENT watched runs (e.g. a codex_ask and a copilot_ask
# at once — they don't share _AGY_LOCK) each get their own window + state instead of
# clobbering one shared one. Sequential runs reuse the "main" slot and its open
# window; a run that starts while "main" is still working gets a fresh id + window.
_MAIN = "main"
_WATCH_RUNS: dict[str, dict] = {}
_WATCH_LOCK = threading.Lock()
_WATCH_SERVER: Optional[tuple] = None  # (httpd, port, thread) singleton
_VIEWER_ALIVE_S = 4.0  # a /events poll within this window means a viewer is still open


def _watch_state(rid, title, start, timeout, backend, prompt, history, last_poll) -> dict:
    return {
        "id": rid,
        "title": title,  # short single-line caption (first prompt line, ≤200 chars)
        "prompt": prompt or title,  # the FULL untruncated prompt, shown in the bubble
        "history": list(history or []),  # prior turns (continue mode); [] for a fresh ask
        "status": "working",  # working | done | error
        "started": start,
        "elapsed": 0.0,
        "timeout": timeout,  # this run's timeout_s, for the time progress bar
        "answer": "",
        "image": "",  # absolute path to a generated image to show, or ""
        "events": [],  # list of {kind, text, t}
        "backend": backend,  # "agy" | "codex" | "copilot" (shown in the header)
        "last_poll": last_poll,  # last /events poll time (0 = never); drives window reuse
    }


_WATCH_IDLE = {
    "status": "idle",
    "started": 0.0,
    "elapsed": 0.0,
    "timeout": 0.0,
    "title": "",
    "prompt": "",
    "history": [],
    "answer": "",
    "image": "",
    "events": [],
    "backend": "agy",
}


def _watch_evict_locked(now: float) -> None:
    """Drop finished, unwatched non-main runs so the map can't grow without bound."""
    stale = [
        r
        for r, s in _WATCH_RUNS.items()
        if r != _MAIN and s["status"] in ("done", "error") and now - s["last_poll"] > 60
    ]
    for rid in stale:
        _WATCH_RUNS.pop(rid, None)


def _watch_begin(
    title: str,
    start: float,
    timeout: float = 0.0,
    backend: str = "agy",
    prompt: str = "",
    history: Optional[list] = None,
) -> str:
    """Start a watched run and return its id. Reuses the "main" slot for the common
    sequential case; a run that begins while "main" is still working gets a fresh id
    (and its own window) so concurrent runs never clobber each other's window."""
    with _WATCH_LOCK:
        _watch_evict_locked(start)
        main = _WATCH_RUNS.get(_MAIN)
        if main is not None and main["status"] == "working":
            rid, last_poll = uuid.uuid4().hex, 0.0
        else:
            # keep the open window's poll time so _open_watch_window can reuse it
            rid, last_poll = _MAIN, (main["last_poll"] if main else 0.0)
        _WATCH_RUNS[rid] = _watch_state(
            rid, title, start, timeout, backend, prompt, history, last_poll
        )
        return rid


def _watch_set_image(rid: str, path: str) -> None:
    with _WATCH_LOCK:
        st = _WATCH_RUNS.get(rid)
        if st is not None:
            st["image"] = path


def _watch_append(rid: str, events: list[dict]) -> None:
    with _WATCH_LOCK:
        st = _WATCH_RUNS.get(rid)
        if st is not None:
            st["events"].extend(events)
            st["elapsed"] = round(time.time() - st["started"], 1)


def _watch_finish(rid: str, status: str, answer: str, elapsed: float) -> None:
    with _WATCH_LOCK:
        st = _WATCH_RUNS.get(rid)
        if st is not None:
            st["status"] = status
            st["answer"] = answer
            st["elapsed"] = round(elapsed, 1)


def _watch_snapshot(rid: str = _MAIN) -> dict:
    with _WATCH_LOCK:
        st = _WATCH_RUNS.get(rid)
        if st is None:
            return dict(_WATCH_IDLE)
        snap = dict(st)
        snap["events"] = list(st["events"])
        return snap


def _watch_mark_poll(rid: str) -> None:
    with _WATCH_LOCK:
        st = _WATCH_RUNS.get(rid)
        if st is not None:
            st["last_poll"] = time.time()


# Per-process secret for the watch viewer. The bridge embeds it in every URL it
# opens; a request without it is refused. Regenerated each run, never persisted.
_WATCH_TOKEN = secrets.token_urlsafe(16)

# Hostnames a watch request may claim. Anything else is a rebinding attempt.
_WATCH_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _watch_authorized(host_header: Optional[str], path: str) -> bool:
    """Whether a watch-server request may be served. Two independent checks.

    The viewer serves the prompts, the answers, and the real commands the agents
    ran. It binds 127.0.0.1 on an ephemeral port, but binding alone is not an
    access boundary, and the server is started lazily and never stopped — one
    watch=true run leaves the port open for the life of the process.

    HOST: the Host header's hostname must be a loopback literal. A browser
    reaches a DNS-rebinding target under the ATTACKER's hostname, so rejecting
    anything that isn't 127.0.0.1/localhost kills that vector outright — it is
    the standard defense, and cheaper than any origin allowlist. A missing Host
    is refused; every browser sends one, and the browser we launch is the only
    client this server has.

    TOKEN: a per-process secret carried as `k` in the query string, compared with
    hmac.compare_digest. The Host check cannot stop another LOCAL process (or
    another user on a shared machine) from simply connecting, since it can send
    whatever Host it likes; the token can, and costs nothing because the bridge
    opens every one of these URLs itself.
    """
    from urllib.parse import parse_qs, urlparse

    host = (host_header or "").strip()
    if not host:
        return False
    if host.startswith("["):  # IPv6 literal, optionally followed by :port
        hostname = host[: host.find("]") + 1]
    else:
        hostname = host.rsplit(":", 1)[0] if ":" in host else host
    if hostname.lower() not in _WATCH_LOCAL_HOSTS:
        return False
    token = parse_qs(urlparse(path).query).get("k", [""])[0]
    return hmac.compare_digest(token, _WATCH_TOKEN)


def _watch_url(port: int, rid: str) -> str:
    """The viewer URL for `rid`, carrying the token the server requires."""
    return f"http://127.0.0.1:{port}/?id={rid}&k={_WATCH_TOKEN}"


def _watch_image_allowed(path: str) -> bool:
    with _WATCH_LOCK:
        return bool(path) and any(s["image"] == path for s in _WATCH_RUNS.values())


class _StreamWatch:
    """Turns agy's `--output-format stream-json` stdout into live watch events.

    The 1.1.8 replacement for _WatchFeed's transcript polling: instead of re-reading
    an undocumented JSONL file on a timer and inferring which conversation is ours,
    we consume the typed NDJSON events agy emits as it works. Verified live that they
    arrive INCREMENTALLY (a 17 s run spread its 18 events over 12.4 s), which is the
    whole premise — a buffered stream would make the viewer useless.

    Event shapes (agy 1.1.8, verified live):
      {"event":"init","conversation_id":…,"init":{cwd,tools,permission_mode}}
      {"event":"step_update","step_update":{conversation_id,step_index,state,
          step_type,tool_name?,text_delta?,tool_info?{name,parameters,output},…}}
      {"event":"result","result":{conversation_id,status,response,usage,…}}

    `state` is ACTIVE then DONE for a given step_index. agent_response text arrives
    as INCREMENTAL `text_delta` fragments that must be concatenated per step (the
    DONE update's delta is usually just the trailing newline), so narration is
    emitted once a step completes rather than per fragment. Tool steps carry
    `tool_info.parameters.CommandLine` as a REAL nested object — unlike the
    transcript, which stores tool args JSON-encoded inside a string and needs
    _clean_tool_arg to unwrap.

    Thread-safe by construction: feed_line runs on the stdout reader thread and only
    touches this instance plus _watch_append (which takes _WATCH_LOCK).
    """

    def __init__(self, rid: str, start: float) -> None:
        self._rid = rid
        self._start = start
        self._text: dict[int, list[str]] = {}  # step_index -> accumulated deltas
        self._emitted: set[int] = set()  # step indices already turned into events
        self.conv_id: Optional[str] = None
        self.result: Optional[dict] = None
        self.saw_event = False  # any well-formed event at all (i.e. agy honoured the flag)

    def _emit(self, kind: str, text: str) -> None:
        if not text:
            return
        _watch_append(
            self._rid, [{"kind": kind, "text": text, "t": round(time.time() - self._start, 1)}]
        )

    def feed_line(self, line: str) -> None:
        """Parse one NDJSON line and append any watch events it implies.

        Never raises: a malformed or unrecognised line is ignored, so an agy that
        changes or drops the format degrades to "no live steps" rather than killing
        the run (the answer still resolves from the result event or the transcript).
        """
        line = (line or "").strip()
        if not line.startswith("{"):
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        kind = event.get("event")
        self.saw_event = True
        if event.get("conversation_id") and not self.conv_id:
            self.conv_id = event["conversation_id"]

        if kind == "result":
            result = event.get("result")
            if isinstance(result, dict):
                self.result = result
                if result.get("conversation_id"):
                    self.conv_id = result["conversation_id"]
            return
        if kind != "step_update":
            return

        step = event.get("step_update")
        if not isinstance(step, dict):
            return
        if step.get("conversation_id") and not self.conv_id:
            self.conv_id = step["conversation_id"]
        idx = step.get("step_index")
        stype, state = step.get("step_type"), step.get("state")

        if stype == "agent_response":
            if isinstance(idx, int) and step.get("text_delta"):
                self._text.setdefault(idx, []).append(step["text_delta"])
            if state == "DONE" and isinstance(idx, int) and idx not in self._emitted:
                self._emitted.add(idx)
                full = "".join(self._text.get(idx, [])).strip()
                if full:
                    self._emit("narration", full.splitlines()[0][:200])
        elif stype == "tool":
            info = step.get("tool_info") or {}
            params = info.get("parameters") or {}
            if state == "ACTIVE":
                cmd = params.get("CommandLine") or info.get("name") or step.get("tool_name") or ""
                self._emit("command", str(cmd)[:200])
            elif state == "DONE":
                self._emit("result", "command finished")


class _WatchFeed:
    """Locks onto this run's conversation and turns new transcript entries into
    rich step events (narration / command / result) appended to the shared watch
    state. For a new conversation it locks onto the first brain dir that appears
    after launch and didn't pre-exist, and never switches away from it.

    The pre-1.1.8 fallback. agy 1.1.8+ runs use _StreamWatch instead, which reads
    agy's own typed event stream rather than scraping this undocumented transcript;
    this path stays for older agy (and is why _clean_tool_arg still exists)."""

    def __init__(self, pinned_conv: Optional[str], start: float, rid: str = _MAIN) -> None:
        self._start = start
        self._rid = rid
        self._pre = set() if pinned_conv else server._existing_conv_names()
        self._conv = pinned_conv
        self._cursor = len(server._transcript_entries(pinned_conv)) if pinned_conv else 0

    @property
    def conv(self) -> Optional[str]:
        return self._conv

    def pump(self) -> None:
        if self._conv is None:
            self._conv = server._newest_new_conv(self._start, self._pre)
            if self._conv is None:
                return
            self._cursor = 0
        entries = server._transcript_entries(self._conv)
        new_events = []
        for entry in entries[self._cursor :]:
            for kind, text in server._entry_to_watch_lines(entry):
                t = round(time.time() - self._start, 1)
                new_events.append({"kind": kind, "text": text, "t": t})
        self._cursor = max(self._cursor, len(entries))
        if new_events:
            _watch_append(self._rid, new_events)


# The single-run viewer: watch_ui's chat window plus a poll loop over /events for
# one run id. It rebuilds itself when `started` changes, so one window is reused
# across sequential runs. __WIN_W__/__WIN_H__ are substituted per request (see
# _watch_html).
_WATCH_HTML = watch_ui.page(
    "Agent Intern",
    r"""
try{window.resizeTo(__WIN_W__,__WIN_H__);}catch(e){}
document.addEventListener("keydown",e=>{
 if(e.key==="Enter"||e.key==="Escape"){try{window.close();}catch(_){}}
});
const RID=Q.get("id")||"main";
let started=null,seen=0,fin=false,fails=0;
function rebuild(s,back){
 resetChat();seen=0;fin=false;
 if(s.status==="idle"){emptyState("Waiting for a watched run…");return;}
 for(const t of s.history||[]){
  if(t.role==="user")userBubble(t.content,"Claude");else botCard(t.content,back,{meta:"earlier"});
 }
 userBubble(s.prompt||s.title||"","Claude");
 newTrace(back==="codex");
}
function finish(s,back){
 fin=true;finishTrace(s.status);
 if(s.image)imageCard("/image?k="+K+"&p="+encodeURIComponent(s.image));
 const err=s.status==="error",meta=back+" · "+fmtS(s.elapsed);
 if(s.answer)botCard(s.answer,err?"Failed":"Answer",{copy:!err,err:err,meta:meta});
}
async function tick(){
 let s=null;
 try{
  const r=await fetch("/events?id="+encodeURIComponent(RID)+"&k="+K,{cache:"no-store"});
  if(r.ok)s=await r.json();
 }catch(e){}
 if(!s){if(++fails>=3&&!fin)setState("lost");}
 else{
  fails=0;
  const back=bkName(s.backend);
  TITLE="Agent Intern · "+back;
  setAgent(s.backend);
  if(s.started!==started){started=s.started;rebuild(s,back);}
  if(s.status==="idle")setState("idle");
  else{
   for(let i=seen;i<s.events.length;i++)addStep(s.events[i]);
   seen=s.events.length;
   if(s.status!=="working"){if(!fin)finish(s,back);setState(s.status,{elapsed:s.elapsed});}
   else setState("working",{start:s.started,timeout:s.timeout,elapsed:s.elapsed});
  }
 }
 setTimeout(tick,fin?1500:fails>=3?2000:400);
}
tick();
""",
    css=".hint{position:fixed;bottom:10px;right:14px;color:var(--tx3);font-size:11px;"
    "pointer-events:none;user-select:none}",
    body="<div class='hint'><kbd>Esc</kbd> to close</div>",
)


def _watch_html() -> str:
    """The watch page with the configured window size substituted for resizeTo."""
    w, h = 600, 820
    try:
        parts = [int(x) for x in _WATCH_WINDOW_SIZE.split(",")]
        if len(parts) == 2:
            w, h = parts
    except ValueError:
        pass
    return _WATCH_HTML.replace("__WIN_W__", str(w)).replace("__WIN_H__", str(h))


def _ensure_watch_server() -> int:
    """Lazily start the localhost watch server (once per process); return its port.

    Binds 127.0.0.1 only — the page and events never leave the local machine.
    Started lazily but never stopped, so once any watch=true run happens the port
    stays open for the life of the process; see _watch_authorized for why that
    makes the Host check and the token load-bearing rather than decorative.
    """
    global _WATCH_SERVER
    if _WATCH_SERVER is not None:
        return _WATCH_SERVER[1]

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence default stderr request logging
            pass

        def _send(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 (http.server API)
            if not _watch_authorized(self.headers.get("Host"), self.path):
                self.send_response(403)
                self.end_headers()
                return
            if self.path.startswith("/events"):
                from urllib.parse import parse_qs, urlparse

                rid = parse_qs(urlparse(self.path).query).get("id", [_MAIN])[0]
                _watch_mark_poll(rid)
                self._send(json.dumps(_watch_snapshot(rid)).encode("utf-8"), "application/json")
            elif self.path.startswith("/image"):
                from urllib.parse import parse_qs, urlparse

                # `p`, not the bare query string: the token now shares the query,
                # so the path must be a named parameter rather than "everything
                # after the ?".
                path = parse_qs(urlparse(self.path).query).get("p", [""])[0]
                fmt = (
                    server._detect_image_format(path)
                    if _watch_image_allowed(path) and os.path.isfile(path)
                    else None
                )
                if fmt:
                    mime = {
                        "JPEG": "image/jpeg",
                        "PNG": "image/png",
                        "GIF": "image/gif",
                        "WEBP": "image/webp",
                    }[fmt]
                    with open(path, "rb") as fh:
                        self._send(fh.read(), mime)
                else:
                    self.send_response(404)
                    self.end_headers()
            else:
                self._send(_watch_html().encode("utf-8"), "text/html; charset=utf-8")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _WATCH_SERVER = (httpd, port, thread)
    server.log.debug("watch server on http://127.0.0.1:%d", port)
    return port


# Small dedicated viewer window. Override "WIDTH,HEIGHT" via AGY_WATCH_WINDOW_SIZE.
_WATCH_WINDOW_SIZE = os.environ.get("AGY_WATCH_WINDOW_SIZE", "560,760")


def _chromium_app_browsers() -> list[str]:
    """Paths to Chromium-based browsers that support `--app` windowed mode, so the
    viewer can open as a small chromeless window instead of a tab. Best-effort."""
    found: list[str] = []
    if os.name == "nt":
        pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        pfx86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(pfx86, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(pfx86, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
        ]
        found += [p for p in candidates if os.path.isfile(p)]
    elif sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ]
        found += [p for p in candidates if os.path.isfile(p)]
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "brave-browser",
        "microsoft-edge",
    ):
        path = shutil.which(name)
        if path:
            found.append(path)
    return found


def _watch_viewer_live(rid: str) -> bool:
    """True if a window is currently polling /events for this run's slot (so a new
    run on the SAME slot should reuse it instead of stacking another window)."""
    with _WATCH_LOCK:
        st = _WATCH_RUNS.get(rid)
        return st is not None and (time.time() - st["last_poll"]) < _VIEWER_ALIVE_S


def _open_watch_window(url: str, rid: str = _MAIN) -> None:
    """Open the watch page in a small, dedicated window. Prefers a Chromium browser
    in `--app` mode (a sized, chromeless window — not a tab); falls back to a normal
    new browser window/tab. Best-effort — never raises.

    Reuses an already-open viewer for this run's slot (detected via recent /events
    polls) so repeated SEQUENTIAL watch calls don't pile up windows; a concurrent run
    got its own id upstream, so it opens its own window here. Set AGY_WATCH_ALWAYS_NEW=1
    to force a fresh window every time."""
    if _watch_viewer_live(rid) and not server._env_truthy("AGY_WATCH_ALWAYS_NEW"):
        server.log.debug("watch viewer already open for %s; reusing instead of a new window", rid)
        return
    for exe in _chromium_app_browsers():
        try:
            proc_tree.popen(
                [exe, f"--app={url}", f"--window-size={_WATCH_WINDOW_SIZE}"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **server._spawn_kwargs(),
            )
            return
        except OSError:
            continue
    try:
        webbrowser.open(url, new=1)  # request a new window (clients may still tab)
    except Exception:  # noqa: BLE001 - viewer is best-effort
        pass


# Last, not first: server imports this module and this module calls back into
# server's helpers (as server.<name>, at call time). Importing server down here
# works whichever of the two is imported first.
import server  # noqa: E402
