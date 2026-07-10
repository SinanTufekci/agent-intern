"""Multi-channel live "watch" dashboard for antigravity_swarm.

The single-worker watch mode in server.py keys its state by run id (_WATCH_RUNS),
one shared server, per-run windows. A swarm runs N workers at once, so this serves
ONE thin dashboard window listing the workers vertically — each row shows the repo,
the prompt, a short snippet of the *latest* operation, and a per-worker time bar.
Clicking a row (or selecting with the keyboard and pressing Enter) opens a
dedicated detail window for that agent — a chat conversation (prompt bubble → live
step trace → Markdown answer), matching the single-worker viewer.
Bound to 127.0.0.1 only. Imported lazily by swarm.py only when watch=True, so
this top-level `from server import` runs after server is fully loaded — no
circular import.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from server import _chromium_app_browsers, _detect_image_format, _env_truthy

_STATE: dict = {"title": "Agent Swarm", "started": 0.0, "timeout": 0.0, "workers": []}
_LOCK = threading.Lock()
_SERVER: Optional[tuple] = None  # (httpd, port)
_CF = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# An open dashboard polls /events a few times a second; track the last poll so
# repeated swarm runs reuse the open window instead of stacking a new one each time.
_LAST_POLL = 0.0
_VIEWER_ALIVE_S = 4.0

# Geometry of the dashboard window, so detail windows can open beside it.
_GEO = {"x": 40, "y": 60, "w": 400, "h": 320}


# ------------------------------------------------------------------- state mutation
def init(
    labels: list[str],
    repos: list[str],
    start: float,
    prompts: Optional[list[str]] = None,
    timeout: float = 0.0,
    backends: Optional[list[str]] = None,
) -> None:
    """Seed dashboard state. `labels` are the short, single-line row captions;
    `prompts` (optional) are the full untruncated prompts shown in each worker's
    detail window (falls back to the label when omitted). `timeout` is the
    per-worker timeout_s, used to draw each row's time progress bar. `backends`
    (optional) is the per-worker backend name shown as a small row badge.
    """
    with _LOCK:
        _STATE["started"] = start
        _STATE["timeout"] = timeout
        _STATE["workers"] = [
            {
                "index": i,
                "label": labels[i],
                "prompt": prompts[i] if prompts and i < len(prompts) else labels[i],
                "repo": repos[i] if i < len(repos) else "",
                "backend": backends[i] if backends and i < len(backends) else "",
                "status": "queued",
                "elapsed": 0.0,
                "events": [],
                "answer": "",
                "image": "",
            }
            for i in range(len(labels))
        ]


def worker_update(index: int, **fields) -> None:
    with _LOCK:
        _STATE["workers"][index].update(fields)


def worker_append(index: int, events: list[dict]) -> None:
    with _LOCK:
        _STATE["workers"][index]["events"].extend(events)


def worker_finish(index: int, status: str, answer: str, elapsed: float, image: str = "") -> None:
    with _LOCK:
        w = _STATE["workers"][index]
        w["status"] = status
        w["answer"] = answer
        w["elapsed"] = round(elapsed, 1)
        if image:
            w["image"] = image


def _snapshot() -> dict:
    with _LOCK:
        return json.loads(json.dumps(_STATE))  # cheap deep copy


def _allowed_images() -> set:
    with _LOCK:
        return {w["image"] for w in _STATE["workers"] if w["image"]}


# ------------------------------------------------------------------- HTTP server
def ensure_server() -> int:
    global _SERVER
    if _SERVER is not None:
        return _SERVER[1]

    class _H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def _send(self, body: bytes, ctype: str):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/events"):
                global _LAST_POLL
                _LAST_POLL = time.time()
                self._send(json.dumps(_snapshot()).encode("utf-8"), "application/json")
            elif self.path.startswith("/open"):
                from urllib.parse import parse_qs, urlparse

                q = parse_qs(urlparse(self.path).query)
                try:
                    idx = int(q.get("i", ["-1"])[0])
                except ValueError:
                    idx = -1
                if 0 <= idx < len(_snapshot()["workers"]):
                    threading.Thread(target=open_worker_window, args=(idx,), daemon=True).start()
                self._send(b'{"ok":true}', "application/json")
            elif self.path.startswith("/worker"):
                self._send(_WORKER_HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path.startswith("/image"):
                from urllib.parse import unquote

                path = unquote(self.path.split("?", 1)[1]) if "?" in self.path else ""
                fmt = (
                    _detect_image_format(path)
                    if path in _allowed_images() and os.path.isfile(path)
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
                self._send(_HTML.encode("utf-8"), "text/html; charset=utf-8")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _SERVER = (httpd, port)
    return port


def _port() -> int:
    return ensure_server()


def _launch(url: str, w: int, h: int, x: Optional[int] = None, y: Optional[int] = None) -> None:
    """Open `url` in a chromeless --app window at a given size/position.

    Uses a fresh, dedicated --user-data-dir per window so Chrome spawns a NEW
    process that actually honors --window-size/--window-position (attaching to an
    already-running profile makes Chrome ignore those flags and reuse old bounds —
    which is why earlier windows opened too wide and stacked on top of each other).
    """
    pos = [f"--window-position={x},{y}"] if x is not None and y is not None else []
    prof = tempfile.mkdtemp(prefix="agy_chrome_")
    flags = [
        f"--app={url}",
        f"--window-size={w},{h}",
        f"--user-data-dir={prof}",
        "--no-first-run",
        "--no-default-browser-check",
        *pos,
    ]
    for exe in _chromium_app_browsers():
        try:
            subprocess.Popen(
                [exe, *flags],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_CF,
            )
            return
        except OSError:
            continue
    try:
        webbrowser.open(url, new=1)
    except Exception:  # noqa: BLE001
        pass


def _dashboard_is_live() -> bool:
    """True if a dashboard polled /events within _VIEWER_ALIVE_S — reuse it rather
    than stacking another window for this run."""
    return (time.time() - _LAST_POLL) < _VIEWER_ALIVE_S


def open_window(n_workers: int) -> None:
    """Open the thin vertical dashboard window (one compact row per worker).

    Reuses an already-open dashboard (detected via recent /events polls) so repeated
    swarm runs don't pile up browser windows; the open page rebuilds itself for the
    new run. Set AGY_WATCH_ALWAYS_NEW=1 to force a fresh window each time."""
    # Fixed, narrow window; panes flex to fill it, so they spread out with few
    # workers and shrink as more are added.
    w, h, x, y = 440, 660, 40, 60
    _GEO.update(x=x, y=y, w=w, h=h)
    url = f"http://127.0.0.1:{_port()}/"
    if _dashboard_is_live() and not _env_truthy("AGY_WATCH_ALWAYS_NEW"):
        print(f"[swarm-watch] reusing open dashboard: {url}", flush=True)
        return
    print(f"[swarm-watch] dashboard: {url}", flush=True)
    _launch(url, w, h, x, y)


def open_worker_window(index: int) -> None:
    """Open a dedicated detail window for one worker, right beside the dashboard."""
    x = _GEO["x"] + _GEO["w"] + 14
    y = _GEO["y"] + index * 28  # slight cascade so multiple detail windows don't fully overlap
    _launch(f"http://127.0.0.1:{_port()}/worker?i={index}", 680, 820, x, y)


# ------------------------------------------------------------------- dashboard page
# Thin vertical list: each row = repo + prompt + a SHORT snippet of the latest op
# + a per-worker time bar. A row is selectable (↑/↓), openable (click or Enter),
# and opens that agent's detail window (full steps) beside the dashboard.
_HTML = """<!doctype html><html lang="en" translate="no"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Swarm</title><style>
:root{--bg:#0a0c10;--fg:#d6d6d6;--dim:#6a7480;--green:#3fdf7f;--cyan:#5cd6e6;--red:#ff6b6b;--bd:#191c22}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg)}
body{color:var(--fg);font:12px/1.5 ui-monospace,"Cascadia Mono",Consolas,monospace;display:flex;flex-direction:column;height:100vh}
::-webkit-scrollbar{width:9px}::-webkit-scrollbar-thumb{background:#23262d;border-radius:6px}
header{display:flex;align-items:center;gap:9px;padding:7px 11px;background:#0d0f14;border-bottom:1px solid var(--bd);flex:none;font-size:11px}
.name{color:var(--green);font-weight:700;text-shadow:0 0 9px rgba(63,223,127,.45)}
.clock{color:#566;font-variant-numeric:tabular-nums}
#tot{margin-left:auto;color:#7c8896;font-variant-numeric:tabular-nums}
.gbar{height:2px;background:#11141a;flex:none}
.gfill{height:100%;width:0;background:linear-gradient(90deg,var(--green),var(--cyan));box-shadow:0 0 8px rgba(92,214,230,.5);transition:width .5s ease}
.grid{display:flex;flex-direction:column;flex:1;overflow:auto}
.pane{position:relative;flex:1 1 0;min-height:54px;padding:9px 12px 12px;border-bottom:1px solid var(--bd);cursor:pointer;user-select:none;display:flex;flex-direction:column;justify-content:center;gap:5px;overflow:hidden;transition:background .15s ease,opacity .3s ease}
.pane:hover{background:#12151c}
.pane.sel{background:#141923;box-shadow:inset 2px 0 0 var(--cyan)}
.pane.done,.pane.error{opacity:.82}
.pane.done:hover,.pane.error:hover,.pane.sel{opacity:1}
.r1{display:flex;align-items:flex-start;gap:7px}
.r1 .dot{margin-top:5px}
.dot{width:7px;height:7px;border-radius:50%;flex:none;transition:background .25s ease}
.queued .dot{background:#556}
.working .dot{background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 1s infinite}
.done .dot{background:var(--cyan);box-shadow:0 0 7px var(--cyan);animation:pop .45s ease}
.error .dot{background:var(--red);box-shadow:0 0 8px var(--red);animation:pop .45s ease}
@keyframes pulse{50%{opacity:.3}}
@keyframes pop{0%{transform:scale(.2)}55%{transform:scale(1.5)}100%{transform:scale(1)}}
.repo{color:#0a0c10;background:var(--green);border-radius:4px;padding:0 5px;font-size:9.5px;font-weight:700;flex:none;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:1px}
.bk{border-radius:4px;padding:0 5px;font-size:9px;font-weight:700;flex:none;margin-top:1px;letter-spacing:.3px}
.bk.codex{color:#0a0c10;background:#7c9cff}
.bk.antigravity{color:#0a0c10;background:#f5b94a}
.bk.copilot{color:#0a0c10;background:#c3a6ff}
.bk.cursor{color:#0a0c10;background:#7ad9a8}
.prompt{color:#e9eef3;font-weight:600;flex:1;min-width:0;overflow:hidden;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;word-break:break-word}
.st{color:var(--dim);font-size:10.5px;flex:none;font-variant-numeric:tabular-nums;margin-top:1px}
.pop{color:var(--green);opacity:.55;flex:none;font-size:11px;margin-top:1px}
.pane:hover .pop{opacity:1;text-shadow:0 0 7px var(--green)}
.sub{display:flex;gap:6px;align-items:baseline;padding-left:14px;color:var(--dim);font-size:11px}
.sub .sym{flex:none;width:9px}
.sub .txt{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sub.command .sym,.sub.command .txt{color:#cdd3d9}.sub.command .sym{color:var(--green)}
.sub.narration .sym,.sub.narration .txt{color:var(--cyan)}
.sub.done .sym,.sub.done .txt{color:var(--cyan)}
.sub.error .sym,.sub.error .txt{color:#ffb3b3}
.spin{color:var(--green);text-shadow:0 0 7px rgba(63,223,127,.6)}
.rbar{position:absolute;left:0;right:0;bottom:0;height:2px;background:#11141a}
.rfill{height:100%;width:0;transition:width .4s linear}
.rfill.working{background:linear-gradient(90deg,rgba(63,223,127,.45),var(--green));background-size:200% 100%;animation:flow 1.1s linear infinite}
.rfill.done{background:var(--cyan)}
.rfill.error{background:var(--red)}
@keyframes flow{from{background-position:200% 0}to{background-position:0 0}}
.foot{flex:none;padding:4px 11px;border-top:1px solid var(--bd);color:#3b414a;font-size:10px;background:#0d0f14;text-align:center}
</style></head><body>
<header><span class="name">Agent Swarm</span><span class="clock" id="clock"></span><span id="tot"></span></header>
<div class="gbar"><div class="gfill" id="gfill"></div></div>
<div class="grid" id="grid"></div>
<div class="foot">↑/↓ select · ↵ open · click a row for its full log</div>
<script>
const SYM={narration:"▸",command:"$",result:"✓",done:"✓",error:"✗"};
const FR="⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏";let fi=0;
let started=null,sel=-1,nWork=0,timeout=0,statuses={};
const $=id=>document.getElementById(id);
function openWorker(i){fetch("/open?i="+i,{cache:"no-store"}).catch(()=>{});}
function esc(s){return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
function cut(s,n){s=s||"";return s.length>n?s.slice(0,n)+"…":s;}
function applySel(){for(let i=0;i<nWork;i++){const p=$("p"+i);if(p)p.classList.toggle("sel",i===sel);}}
function build(ws){
 const g=$("grid");g.innerHTML="";statuses={};
 ws.forEach(w=>{
  const p=document.createElement("div");p.className="pane "+w.status;p.id="p"+w.index;
  p.title="click to open this agent's full step log";p.onclick=()=>openWorker(w.index);
  p.innerHTML="<div class='r1'><span class='dot'></span>"+
   (w.backend?"<span class='bk "+w.backend+"' title='"+esc(w.backend)+"'>"+(w.backend==='codex'?'codex':w.backend==='copilot'?'copilot':w.backend==='cursor'?'cursor':'agy')+"</span>":"")+
   (w.repo?"<span class='repo' title='"+esc(w.repo)+"'>"+esc(w.repo)+"</span>":"")+
   "<span class='prompt' title='"+esc(w.label)+"'>"+esc(w.label||("Worker "+w.index))+"</span>"+
   "<span class='st' id='st"+w.index+"'></span><span class='pop'>↗</span></div>"+
   "<div class='sub' id='sub"+w.index+"'></div>"+
   "<div class='rbar'><div class='rfill' id='rf"+w.index+"'></div></div>";
  g.appendChild(p);statuses[w.index]=w.status;
 });
 if(sel>=ws.length)sel=ws.length-1;
 applySel();
}
document.addEventListener("keydown",e=>{
 if(!nWork)return;
 if(e.key==="ArrowDown"||e.key==="ArrowUp"){
  e.preventDefault();
  sel=(sel<0)?0:sel+(e.key==="ArrowDown"?1:-1);
  if(sel<0)sel=0;if(sel>=nWork)sel=nWork-1;
  applySel();const el=$("p"+sel);if(el)el.scrollIntoView({block:"nearest"});
 }else if(e.key==="Enter"&&sel>=0){openWorker(sel);}
});
async function tick(){
 fi=(fi+1)%FR.length;
 try{
  const s=await(await fetch("/events",{cache:"no-store"})).json();
  if(s.started!==started){started=s.started;nWork=s.workers.length;timeout=s.timeout||0;build(s.workers);}
  s.workers.forEach(w=>{
   const p=$("p"+w.index);
   if(p&&statuses[w.index]!==w.status){statuses[w.index]=w.status;p.className="pane "+w.status;applySel();}
   const st=$("st"+w.index);
   if(st)st.textContent=w.status==="queued"?"queued":
     (w.status==="working"?w.elapsed.toFixed(1)+"s":w.status+" "+w.elapsed.toFixed(1)+"s");
   const sub=$("sub"+w.index);
   if(sub){
    const e=w.events.length?w.events[w.events.length-1]:null;
    if(w.status==="working"){
     sub.className="sub "+(e?e.kind:"");
     sub.innerHTML="<span class='sym spin'>"+FR[fi]+"</span><span class='txt'>"+
       esc(cut(e?e.text:"starting…",54))+"</span>";
    }else if(w.status==="done"||w.status==="error"){
     const k=w.status;sub.className="sub "+k;
     sub.innerHTML="<span class='sym'>"+SYM[k]+"</span><span class='txt'>"+
       esc(cut((w.answer||"").split("\\n")[0]||w.status,54))+"</span>";
    }else if(e){
     sub.className="sub "+e.kind;
     sub.innerHTML="<span class='sym'>"+(SYM[e.kind]||"·")+"</span><span class='txt'>"+
       esc(cut(e.text,54))+"</span>";
    }else{sub.className="sub";sub.innerHTML="<span class='txt' style='opacity:.5'>queued…</span>";}
   }
   const rf=$("rf"+w.index);
   if(rf){
    let frac=0;
    if(w.status==="done"||w.status==="error")frac=1;
    else if(w.status==="working")frac=timeout>0?Math.min(w.elapsed/timeout,.98):0.06;
    rf.className="rfill "+w.status;rf.style.width=Math.round(frac*100)+"%";
   }
  });
  const done=s.workers.filter(w=>w.status==="done"||w.status==="error").length;
  $("gfill").style.width=(nWork?Math.round(done/nWork*100):0)+"%";
  $("tot").textContent=done+"/"+nWork+" done";
  const el=started?(Date.now()/1000-started):0;
  $("clock").textContent=el>0?el.toFixed(0)+"s":"";
 }catch(e){}
 setTimeout(tick,400);
}
tick();
</script></body></html>"""


# Dedicated single-worker detail page (opened when a row is clicked / Enter). Shows
# the repo + expandable full prompt, a time progress bar, the step-by-step stream
# revealed with a typewriter, and the final Markdown answer / image (with a copy
# button). Mirrors the single-worker viewer in server.py.
_WORKER_HTML = """<!doctype html><html lang="en" translate="no"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Intern</title><style>
:root{--bg:#0a0c10;--fg:#d6d6d6;--dim:#6a7480;--green:#3fdf7f;--cyan:#5cd6e6;--red:#ff6b6b;--bd:#191c22;--code:#06080b;--ubg:#13251c;--ubd:#2a5a41;--uc:#e9f6ee}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg)}
body{color:var(--fg);font:13px/1.6 ui-monospace,"Cascadia Mono",Consolas,monospace}
::-webkit-scrollbar{width:9px}::-webkit-scrollbar-thumb{background:#23262d;border-radius:6px}
.top{position:sticky;top:0;z-index:3;background:var(--bg)}
header{display:flex;align-items:center;gap:8px;padding:9px 14px;background:#0d0f14;border-bottom:1px solid var(--bd)}
.name{color:var(--green);font-weight:700;text-shadow:0 0 10px rgba(63,223,127,.4)}
.repo{color:#0a0c10;background:var(--green);border-radius:4px;padding:0 6px;font-size:10px;font-weight:700}
.bk{border-radius:4px;padding:0 6px;font-size:9.5px;font-weight:700;letter-spacing:.3px;color:#0a0c10}
.bk.antigravity{background:#f5b94a}.bk.codex{background:#7c9cff}.bk.copilot{background:#c3a6ff}.bk.cursor{background:#7ad9a8}
.pill{margin-left:auto;display:flex;align-items:center;gap:7px;font-size:12px;color:var(--dim);font-variant-numeric:tabular-nums}
.dot{width:8px;height:8px;border-radius:50%;flex:none;display:none}
.dot.done{background:var(--cyan);box-shadow:0 0 7px var(--cyan);animation:pop .45s ease}
.dot.error{background:var(--red);box-shadow:0 0 8px var(--red);animation:pop .45s ease}
@keyframes pop{0%{transform:scale(.2)}55%{transform:scale(1.5)}100%{transform:scale(1)}}
.spin{color:var(--green);display:inline-block;width:9px;text-align:center;text-shadow:0 0 8px rgba(63,223,127,.6)}
.gbar{height:2px;background:#11141a}
.gfill{height:100%;width:0;background:linear-gradient(90deg,var(--green),var(--cyan));box-shadow:0 0 8px rgba(92,214,230,.5);transition:width .4s linear}
#chat{max-width:960px;margin:0 auto;padding:16px 14px 46px;display:flex;flex-direction:column;gap:11px}
.msg{display:flex;max-width:100%;animation:rise .28s ease both}
@keyframes rise{from{opacity:0;transform:translateY(7px)}}
.msg.user{justify-content:flex-end}.msg.bot{justify-content:flex-start}
.role{font-size:9px;letter-spacing:1.4px;font-weight:700;opacity:.7;margin:0 3px 3px}
.wrap{display:flex;flex-direction:column;max-width:84%}
.msg.user .wrap{align-items:flex-end}
.bubble{position:relative;padding:9px 13px;border-radius:15px;word-break:break-word;box-shadow:0 1px 2px rgba(0,0,0,.25)}
.bubble.user{background:var(--ubg);border:1px solid var(--ubd);color:var(--uc);border-bottom-right-radius:5px}
.bubble.bot{background:#0c0e13;border:1px solid var(--bd);border-bottom-left-radius:5px}
.bubble.bot.err{border-color:#5a2a2a}
.btext{white-space:pre-wrap;word-break:break-word}
.bubble.user.clampable .btext{max-height:7.4em;overflow:hidden;-webkit-mask-image:linear-gradient(180deg,#000 72%,transparent)}
.bubble.user.expanded .btext{max-height:60vh;overflow:auto;-webkit-mask-image:none}
.exp{margin-top:6px;font-size:10.5px;color:var(--cyan);cursor:pointer;user-select:none;opacity:.85}
.exp:hover{opacity:1}
.trace{background:#0b0d12;border:1px solid var(--bd);border-radius:13px;border-bottom-left-radius:5px;overflow:hidden;max-width:84%}
.trace-head{display:flex;align-items:center;gap:8px;padding:7px 12px;cursor:pointer;color:var(--dim);font-size:11px}
.trace-head:hover{background:#0f1218}
.trace-body{padding:1px 12px 9px;display:flex;flex-direction:column;gap:3px}
.trace.collapsed .trace-body{display:none}
.chev{margin-left:auto;color:var(--green);opacity:.7;transition:transform .2s}
.trace.collapsed .chev{transform:rotate(-90deg)}
.ty{display:inline-flex;gap:3px;align-items:center}
.ty i{width:4px;height:4px;border-radius:50%;background:var(--green);opacity:.4;animation:ty 1s infinite}
.ty i:nth-child(2){animation-delay:.16s}.ty i:nth-child(3){animation-delay:.32s}
@keyframes ty{0%,60%,100%{opacity:.35}30%{opacity:1}}
.step{display:flex;gap:8px;align-items:baseline;font-size:11.5px;animation:rise .2s ease both}
.step .sym{width:11px;flex:none}
.step .txt{white-space:pre-wrap;word-break:break-word;color:#c7ccd2}
.step.command .sym{color:var(--green)}.step.command .txt{color:#eaeef2}
.step.narration .sym,.step.narration .txt{color:var(--cyan)}
.step.result .sym,.step.result .txt{color:var(--green);opacity:.55}
.md .h{font-weight:700;margin:12px 0 5px;color:#cdd9e5}
.md .h1{font-size:16px;color:#fff}.md .h2{font-size:14px}.md .h3{font-size:12.5px;color:var(--green)}
.md .p{margin:3px 0;white-space:pre-wrap;word-break:break-word}
.md .li{display:flex;gap:8px;margin:2px 0}
.md .bul{color:var(--green);flex:none;min-width:14px;text-align:right}
.md .lit{white-space:pre-wrap;word-break:break-word}
.md pre.code{background:var(--code);border-left:2px solid var(--green);border-radius:4px;padding:9px 11px;margin:7px 0;overflow:auto;white-space:pre;color:#e9efe9}
.md code{background:#16191f;padding:1px 5px;border-radius:4px;color:#9fe6ad}
.md .lnk{color:var(--cyan);border-bottom:1px dotted #2a6b73}
.md strong{color:#fff}
.md .copy{position:absolute;top:7px;right:8px;background:#0e1218;border:1px solid var(--bd);color:var(--dim);font:inherit;font-size:10px;padding:2px 8px;border-radius:5px;cursor:pointer;opacity:0;transition:opacity .15s,color .15s,border-color .15s}
.bubble.bot:hover .copy{opacity:.92}
.md .copy:hover{color:var(--green);border-color:#2a3340}
.shot{max-width:100%;border:1px solid var(--bd);border-radius:12px;display:block;animation:rise .3s ease both}
.jump{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:#12161d;border:1px solid #2a3340;color:var(--cyan);font-size:11px;padding:5px 13px;border-radius:20px;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.5);animation:rise .3s;z-index:4}
</style></head><body>
<div class="top">
<header><span class="name">Agent Intern</span>
<span class="bk" id="bk" style="display:none"></span>
<span class="repo" id="repo" style="display:none"></span>
<span class="pill"><span class="dot" id="dot"></span><span class="spin" id="spin"></span><span id="st"></span></span></header>
<div class="gbar"><div class="gfill" id="gfill"></div></div>
</div>
<div id="chat"></div>
<div class="jump" id="jump" style="display:none">↓ jump to latest</div>
<script>
const SYM={narration:"▸",command:"$",result:"✓"};
const IDX=parseInt(new URLSearchParams(location.search).get("i")||"0",10);
let started=null,seen=0,fin=false,follow=true,timeout=0,traceEl=null,traceBody=null;
const $=id=>document.getElementById(id);
const chat=()=>$("chat");
function esc(s){return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
function toBottom(){window.scrollTo(0,document.body.scrollHeight);}
function maybeBottom(){if(follow)toBottom();}
window.addEventListener("scroll",()=>{
 follow=window.innerHeight+window.scrollY>=document.body.scrollHeight-44;
 $("jump").style.display=follow?"none":"";
});
$("jump").onclick=()=>{follow=true;$("jump").style.display="none";toBottom();};
const FR="⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏";let fi=0,spinT=null;
function startSpin(){if(spinT)return;spinT=setInterval(()=>{$("spin").textContent=FR[fi=(fi+1)%FR.length];},80);}
function stopSpin(){if(spinT){clearInterval(spinT);spinT=null;}$("spin").textContent="";}
function inl(s){
 return s.replace(/\\[([^\\]]+)\\]\\(([^)]+)\\)/g,"<span class='lnk'>$1</span>")
         .replace(/`([^`]+)`/g,(m,c)=>"<code>"+c+"</code>")
         .replace(/\\*\\*([^*]+)\\*\\*/g,"<strong>$1</strong>");
}
function md(src){
 const lines=esc(src).split("\\n"),out=[];let inC=false,code="";
 for(const ln of lines){
  const f=ln.match(/^```(\\w*)\\s*$/);
  if(f){if(!inC){inC=true;code="";}else{inC=false;
   out.push("<pre class='code'>"+code.replace(/\\n$/,"")+"</pre>");}continue;}
  if(inC){code+=ln+"\\n";continue;}
  const h=ln.match(/^(#{1,6})\\s+(.*)$/);
  if(h){out.push("<div class='h h"+h[1].length+"'>"+inl(h[2])+"</div>");continue;}
  const b=ln.match(/^\\s*[-*]\\s+(.*)$/);
  if(b){out.push("<div class='li'><span class='bul'>•</span>"+
   "<span class='lit'>"+inl(b[1])+"</span></div>");continue;}
  const n=ln.match(/^\\s*(\\d+)\\.\\s+(.*)$/);
  if(n){out.push("<div class='li'><span class='bul'>"+n[1]+".</span>"+
   "<span class='lit'>"+inl(n[2])+"</span></div>");continue;}
  if(ln.trim()==="")continue;
  out.push("<div class='p'>"+inl(ln)+"</div>");
 }
 if(inC)out.push("<pre class='code'>"+code+"</pre>");
 return out.join("");
}
function copyText(txt,btn){
 navigator.clipboard.writeText(txt).then(()=>{
  const o=btn.textContent;btn.textContent="copied ✓";setTimeout(()=>btn.textContent=o,1200);
 }).catch(()=>{});
}
function userBubble(text){
 const m=document.createElement("div");m.className="msg user";
 const wrap=document.createElement("div");wrap.className="wrap";
 const r=document.createElement("div");r.className="role";r.textContent="CLAUDE";wrap.appendChild(r);
 const b=document.createElement("div");b.className="bubble user clampable";
 const t=document.createElement("div");t.className="btext";t.textContent=text||"";
 b.appendChild(t);wrap.appendChild(b);m.appendChild(wrap);chat().appendChild(m);
 requestAnimationFrame(()=>{
  if(t.scrollHeight>t.clientHeight+2){
   const x=document.createElement("div");x.className="exp";x.textContent="show more ▾";
   x.onclick=()=>{const e=b.classList.toggle("expanded");x.textContent=e?"show less ▴":"show more ▾";maybeBottom();};
   b.appendChild(x);
  }else{b.classList.remove("clampable");}
  maybeBottom();
 });
}
function newTrace(){
 const m=document.createElement("div");m.className="msg bot";
 const tr=document.createElement("div");tr.className="trace";
 tr.innerHTML="<div class='trace-head'><span class='ty'><i></i><i></i><i></i></span>"+
  "<span class='tlabel'>working…</span><span class='chev'>▾</span></div>"+
  "<div class='trace-body'></div>";
 m.appendChild(tr);chat().appendChild(m);
 tr.querySelector(".trace-head").onclick=()=>tr.classList.toggle("collapsed");
 traceEl=tr;traceBody=tr.querySelector(".trace-body");
}
function addStep(e){
 if(!traceBody)return;
 const r=document.createElement("div");r.className="step "+e.kind;
 r.innerHTML="<span class='sym'></span><span class='txt'></span>";
 r.querySelector(".sym").textContent=SYM[e.kind]||"·";
 r.querySelector(".txt").textContent=e.text;
 traceBody.appendChild(r);maybeBottom();
}
function rebuild(w,back){
 chat().innerHTML="";seen=0;fin=false;follow=true;traceEl=null;traceBody=null;
 $("dot").style.display="none";$("dot").className="dot";
 $("gfill").style.width="0";$("gfill").style.background="";
 $("jump").style.display="none";startSpin();
 userBubble(w.prompt||w.label||"");
 newTrace();
}
function finish(w,back){
 fin=true;stopSpin();
 $("dot").className="dot "+w.status;$("dot").style.display="";
 $("gfill").style.width="100%";
 if(w.status==="error")$("gfill").style.background="var(--red)";
 if(traceEl){
  traceEl.classList.add("collapsed");
  const lbl=traceEl.querySelector(".tlabel");if(lbl)lbl.textContent=seen+" steps ✓";
  const ty=traceEl.querySelector(".ty");if(ty)ty.remove();
 }
 if(w.image){
  const m=document.createElement("div");m.className="msg bot";
  const wrap=document.createElement("div");wrap.className="wrap";
  const im=document.createElement("img");im.className="shot";
  im.onload=maybeBottom;im.src="/image?"+encodeURIComponent(w.image);
  wrap.appendChild(im);m.appendChild(wrap);chat().appendChild(m);
 }
 if(w.answer){
  const m=document.createElement("div");m.className="msg bot";
  const wrap=document.createElement("div");wrap.className="wrap";
  const r=document.createElement("div");r.className="role";r.textContent=back.toUpperCase();wrap.appendChild(r);
  const b=document.createElement("div");b.className="bubble bot md"+(w.status==="error"?" err":"");
  b.innerHTML=md(w.answer);
  const cp=document.createElement("button");cp.className="copy";cp.textContent="copy";
  cp.onclick=()=>copyText(w.answer,cp);b.appendChild(cp);
  wrap.appendChild(b);m.appendChild(wrap);chat().appendChild(m);
 }
 maybeBottom();
}
async function tick(){
 try{
  const s=await(await fetch("/events",{cache:"no-store"})).json();
  const w=s.workers[IDX];
  if(w){
   const back=w.backend||"agy";const bname=back==="codex"?"codex":back==="copilot"?"copilot":back==="cursor"?"cursor":"agy";
   if(s.started!==started){started=s.started;timeout=s.timeout||0;rebuild(w,bname);}
   document.title="Intern · "+(w.repo?w.repo+" · ":"")+(w.label||("Worker "+IDX));
   if(w.repo){$("repo").style.display="";$("repo").textContent=w.repo;}
   if(w.backend){$("bk").style.display="";$("bk").className="bk "+w.backend;$("bk").textContent=bname;}
   if(!fin){
    $("st").textContent=w.status==="queued"?"queued":(w.status||"working")+" · "+(w.elapsed||0).toFixed(1)+"s";
    let frac=w.status==="working"?(timeout>0?Math.min(w.elapsed/timeout,.98):0.06):0.03;
    $("gfill").style.width=Math.round(frac*100)+"%";
   }
   for(let i=seen;i<w.events.length;i++)addStep(w.events[i]);
   seen=w.events.length;
   if((w.status==="done"||w.status==="error")&&!fin){
    $("st").textContent=w.status+" · "+(w.elapsed||0).toFixed(1)+"s";finish(w,bname);}
  }
 }catch(e){}
 setTimeout(tick,fin?1500:400);
}
tick();
</script></body></html>"""
