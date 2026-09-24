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

import proc_tree
import server
import watch_ui
from server import (
    _chromium_app_browsers,
    _detect_image_format,
    _env_truthy,
    _watch_authorized,
)

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
            # Same guard as the single-run viewer: loopback Host + the process
            # token. This dashboard exposes every worker's prompt and steps at
            # once, so it is the bigger of the two leaks. See _watch_authorized.
            if not _watch_authorized(self.headers.get("Host"), self.path):
                self.send_response(403)
                self.end_headers()
                return
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
                from urllib.parse import parse_qs, urlparse

                # `p`, not the bare query string: the token shares the query now.
                path = parse_qs(urlparse(self.path).query).get("p", [""])[0]
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
            proc_tree.popen(
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
    # Fixed, narrow window, tall enough for five worker cards; more scroll.
    w, h, x, y = 440, 720, 40, 60
    _GEO.update(x=x, y=y, w=w, h=h)
    url = f"http://127.0.0.1:{_port()}/?k={server._WATCH_TOKEN}"
    if _dashboard_is_live() and not _env_truthy("AGY_WATCH_ALWAYS_NEW"):
        print(f"[swarm-watch] reusing open dashboard: {url}", flush=True)
        return
    print(f"[swarm-watch] dashboard: {url}", flush=True)
    _launch(url, w, h, x, y)


def open_worker_window(index: int) -> None:
    """Open a dedicated detail window for one worker, right beside the dashboard."""
    x = _GEO["x"] + _GEO["w"] + 14
    y = _GEO["y"] + index * 28  # slight cascade so multiple detail windows don't fully overlap
    _launch(f"http://127.0.0.1:{_port()}/worker?i={index}&k={server._WATCH_TOKEN}", 680, 820, x, y)


# ------------------------------------------------------------------- dashboard page
# One card per worker: backend avatar + prompt + status chip, then its latest step
# and a time bar. A card is selectable (↑/↓), openable (click or Enter), and opens
# that agent's detail window beside the dashboard. The header counts workers by
# state; its clock stops once every worker has finished. Built on watch_ui's base
# styles and helpers so it matches the chat windows.
_HTML = (
    """<!doctype html><html lang="en" translate="no"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Swarm</title><style>"""
    + watch_ui.BASE_CSS
    + r"""
body{display:flex;flex-direction:column;height:100vh;overflow:hidden}
header{flex:none;padding:12px 12px 11px;background:rgba(10,12,17,.84);border-bottom:1px solid var(--bd2)}
.hrow{display:flex;align-items:center;gap:10px}
.logo{flex:none;width:30px;height:30px;border-radius:9px;display:grid;place-items:center;color:#062011;background:linear-gradient(135deg,var(--green),var(--cyan));box-shadow:0 4px 16px rgba(63,223,127,.28)}
.logo svg{width:15px;height:15px}
.name{font-weight:650;font-size:14px}
.subt{font-size:11.5px;color:var(--tx3)}
.clock{margin-left:auto;display:flex;align-items:center;gap:6px;font-weight:600;font-size:12px;color:var(--tx2);font-variant-numeric:tabular-nums;background:var(--s2);border:1px solid var(--bd);border-radius:999px;padding:4px 11px 4px 9px;transition:all .3s}
.clock svg{width:13px;height:13px}
.clock.fin{color:#c3f3f9;background:rgba(92,214,230,.09);border-color:rgba(92,214,230,.32)}
.clock.fin.bad{color:#ffc9c9;background:rgba(255,107,107,.1);border-color:rgba(255,107,107,.34)}
.clock.lost{color:#ffe2ab;background:rgba(245,185,74,.1);border-color:rgba(245,185,74,.34)}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-top:11px}
.stat{display:flex;align-items:baseline;gap:6px;background:var(--s1);border:1px solid var(--bd2);border-radius:9px;padding:4px 9px;transition:border-color .3s}
.stat b{font-size:15px;line-height:1.3;font-weight:700;font-variant-numeric:tabular-nums;color:#3a414f;transition:color .3s}
.stat span{font-size:10.5px;color:var(--tx3)}
.stat.on.run{border-color:rgba(63,223,127,.28)}.stat.on.run b{color:var(--green)}
.stat.on.q b{color:var(--tx)}
.stat.on.ok{border-color:rgba(92,214,230,.26)}.stat.on.ok b{color:var(--cyan)}
.stat.on.err{border-color:rgba(255,107,107,.3)}.stat.on.err b{color:var(--red)}
.seg{display:flex;height:4px;border-radius:4px;background:var(--s3);overflow:hidden;margin-top:10px}
.seg i{display:block;height:100%;width:0;transition:width .6s cubic-bezier(.2,.7,.2,1)}
#gdone{background:linear-gradient(90deg,var(--green),var(--cyan))}#gerr{background:var(--red)}
.list{flex:1;overflow:auto;padding:10px 12px 12px;display:flex;flex-direction:column;gap:8px}
.card{position:relative;flex:none;background:var(--s1);border:1px solid var(--bd);border-radius:12px;padding:10px 12px 11px;cursor:pointer;user-select:none;transition:border-color .15s,background .15s,box-shadow .15s;animation:rise .35s ease both}
.card:hover{background:var(--s2);border-color:#313949}
.card.sel{border-color:rgba(92,214,230,.6);box-shadow:0 0 0 3px rgba(92,214,230,.13)}
.card.error{border-color:rgba(255,107,107,.3)}
.c1{display:flex;gap:10px;align-items:flex-start}
.prompt{flex:1;min-width:0;font-weight:600;font-size:12.5px;line-height:1.4;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;word-break:break-word;margin-top:1px}
.card.done .prompt,.card.error .prompt{color:var(--tx2)}
.stc{flex:none;display:flex;align-items:center;gap:5px;font-weight:600;font-size:11px;line-height:1;padding:4px 8px;border-radius:999px;background:var(--s2);border:1px solid var(--bd);color:var(--tx3);font-variant-numeric:tabular-nums;white-space:nowrap}
.stc svg{width:11px;height:11px;animation:pop .4s ease}.stc .ring{width:9px;height:9px;border-width:1.6px}
.working .stc{color:#c3f7d8;background:rgba(63,223,127,.09);border-color:rgba(63,223,127,.3)}
.done .stc{color:#c3f3f9;background:rgba(92,214,230,.08);border-color:rgba(92,214,230,.28)}
.error .stc{color:#ffc9c9;background:rgba(255,107,107,.1);border-color:rgba(255,107,107,.32)}
.c2{display:flex;align-items:center;gap:7px;margin:7px 0 0 36px;min-width:0;font-size:11.5px;color:var(--tx3)}
.last{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.last.narration{color:var(--tx2)}
.last.command{font:11px var(--mono);color:var(--tx)}.last.command b{color:var(--green);margin-right:6px}
.last.done{color:#a7e9f2}.last.error{color:#ffb8b8}
.cnt{flex:none;font-size:10.5px;font-variant-numeric:tabular-nums}
.bar{height:3px;border-radius:3px;background:var(--s3);margin:9px 0 0 36px;overflow:hidden}
.bar i{display:block;height:100%;width:0;border-radius:3px;transition:width .5s linear}
.bar i.working{background:linear-gradient(90deg,rgba(63,223,127,.45),var(--green));background-size:200% 100%;animation:flow 1.2s linear infinite}
.bar i.working.warn{background:linear-gradient(90deg,rgba(245,185,74,.45),var(--amber));background-size:200% 100%}
.bar i.done{background:var(--cyan)}.bar i.error{background:var(--red)}
@keyframes flow{from{background-position:200% 0}to{background-position:0 0}}
.foot{flex:none;display:flex;justify-content:center;align-items:center;gap:6px;padding:7px 11px;border-top:1px solid var(--bd2);color:var(--tx3);font-size:11px;background:rgba(10,12,17,.84)}
.foot .sep{margin:0 4px;opacity:.5}
</style></head><body>
<header>
 <div class="hrow">
  <div class="logo"><svg viewBox="0 0 16 16"><g fill="currentColor"><rect x="2" y="2" width="5" height="5" rx="1.5"/><rect x="9" y="2" width="5" height="5" rx="1.5"/><rect x="2" y="9" width="5" height="5" rx="1.5"/><rect x="9" y="9" width="5" height="5" rx="1.5" opacity=".55"/></g></svg></div>
  <div><div class="name">Agent Swarm</div><div class="subt" id="subt">connecting…</div></div>
  <div class="clock" id="clockw"><span id="clockic"></span><span id="clock">0s</span></div>
 </div>
 <div class="stats">
  <div class="stat run" id="n_working"><b>0</b><span>running</span></div>
  <div class="stat q" id="n_queued"><b>0</b><span>queued</span></div>
  <div class="stat ok" id="n_done"><b>0</b><span>done</span></div>
  <div class="stat err" id="n_error"><b>0</b><span>failed</span></div>
 </div>
 <div class="seg" id="seg"><i id="gdone"></i><i id="gerr"></i></div>
</header>
<div class="list" id="grid"></div>
<div class="foot"><kbd>↑</kbd><kbd>↓</kbd> select<span class="sep">·</span><kbd>↵</kbd> open<span class="sep">·</span>or click a card for its full log</div>
<script>"""
    + watch_ui.BASE_JS
    + r"""
let started=null,sel=-1,nWork=0,timeout=0,statuses={},fails=0;
function openWorker(i){fetch("/open?i="+i+"&k="+K,{cache:"no-store"}).catch(()=>{});}
function now(){return Date.now()/1000;}
// A working worker's clock runs locally from its start time, so it moves between
// the (coarse) moments its backend reports progress.
function elapsedOf(w){return w.status==="working"&&w.started?Math.max(w.elapsed||0,now()-w.started):(w.elapsed||0);}
function applySel(){for(let i=0;i<nWork;i++){const p=$("p"+i);if(p)p.classList.toggle("sel",i===sel);}}
function setHtml(node,key,html){if(node.dataset.k!==key){node.dataset.k=key;node.innerHTML=html;}}
function build(ws){
 const g=$("grid");g.innerHTML="";statuses={};
 ws.forEach(w=>{
  const c=el("div","card "+w.status);c.id="p"+w.index;c.style.animationDelay=(w.index*45)+"ms";
  c.title="Open this agent's full log";c.onclick=()=>openWorker(w.index);
  const r1=el("div","c1"),pr=el("div","prompt",w.label||("Worker "+w.index));pr.title=w.label||"";
  const st=el("div","stc");st.id="st"+w.index;
  r1.append(avatar(w.backend||"agy"),pr,st);
  const r2=el("div","c2");
  if(w.repo){const rp=el("span","repo",w.repo);rp.title=w.repo;r2.appendChild(rp);}
  const last=el("span","last");last.id="sub"+w.index;
  const cnt=el("span","cnt");cnt.id="cnt"+w.index;
  r2.append(last,cnt);
  const bar=el("div","bar"),fill=el("i");fill.id="rf"+w.index;bar.appendChild(fill);
  c.append(r1,r2,bar);g.appendChild(c);statuses[w.index]=w.status;
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
  applySel();const c=$("p"+sel);if(c)c.scrollIntoView({block:"nearest"});
 }else if(e.key==="Enter"&&sel>=0){openWorker(sel);}
});
// The step a card shows: the newest one, unless it is a bare "done" that only
// closes the command before it (that command is the more useful line to show).
function latestStep(evs){
 for(let i=evs.length-1;i>=0;i--){
  const e=evs[i];
  if(e.kind!=="result")return e;
  if(!/^(done|command finished)$/i.test(e.text||""))return {kind:"error",text:e.text};
 }
 return null;
}
function paintWorker(w){
 const c=$("p"+w.index);if(!c)return;
 if(statuses[w.index]!==w.status){statuses[w.index]=w.status;c.className="card "+w.status;applySel();}
 const t=elapsedOf(w),budget=w.timeout||timeout;
 const st=$("st"+w.index);
 setHtml(st,w.status,({working:RING,done:IC.check,error:IC.x}[w.status]||IC.clock)+"<span></span>");
 st.lastChild.textContent=w.status==="queued"?"Queued":fmtS(t);
 let cls="",txt;
 if(w.status==="done"||w.status==="error"){cls=w.status;txt=(w.answer||"").split("\n")[0]||(w.status==="done"?"Done":"Failed");}
 else if(w.status==="working"){const e=latestStep(w.events);cls=e?e.kind:"narration";txt=e?e.text:"Starting…";}
 else txt="Waiting for a free slot…";
 const last=$("sub"+w.index);
 if(last._k!==cls+"\n"+txt){
  last._k=cls+"\n"+txt;last.className="last "+cls;last.textContent="";last.title=txt;
  if(cls==="command")last.appendChild(el("b",null,"$"));
  last.appendChild(document.createTextNode(txt));
 }
 const steps=w.events.filter(e=>e.kind!=="result").length;
 $("cnt"+w.index).textContent=steps?steps+" step"+(steps===1?"":"s"):"";
 const rf=$("rf"+w.index);
 let frac=0;
 if(w.status==="done"||w.status==="error")frac=1;
 else if(w.status==="working")frac=budget>0?Math.min(t/budget,.98):0.06;
 rf.className=w.status+(w.status==="working"&&frac>.75?" warn":"");
 rf.style.width=(frac*100).toFixed(1)+"%";
 rf.parentNode.title=w.status==="working"&&budget>0?fmtS(t)+" of the "+fmtS(budget)+" time budget":"";
}
function header(s){
 const n={working:0,queued:0,done:0,error:0};
 s.workers.forEach(w=>{if(w.status in n)n[w.status]++;});
 for(const k in n){const b=$("n_"+k);b.classList.toggle("on",n[k]>0);b.firstChild.textContent=n[k];}
 $("gdone").style.width=(nWork?n.done/nWork*100:0)+"%";
 $("gerr").style.width=(nWork?n.error/nWork*100:0)+"%";
 const fin=n.done+n.error,all=nWork>0&&fin===nWork;
 // Once every worker has finished, the clock shows when the last one did.
 let t=started?now()-started:0;
 if(all){t=0;s.workers.forEach(w=>{if(w.started)t=Math.max(t,w.started+(w.elapsed||0)-started);});}
 $("clockw").className="clock"+(all?" fin"+(n.error?" bad":""):"");
 setHtml($("clockic"),all?(n.error?"x":"ok"):"clock",all?(n.error?IC.x:IC.check):IC.clock);
 $("clock").textContent=t>0?fmtS(t).replace(/\.\ds$/,"s"):"0s";
 const kinds=new Set(s.workers.map(w=>bkName(w.backend)));
 $("subt").textContent=nWork+" agent"+(nWork===1?"":"s")+" · "+kinds.size+" backend"+(kinds.size===1?"":"s")+(all?" · finished":"");
 document.title=(all?(n.error?"✗ ":"✓ "):"● ")+fin+"/"+nWork+" · Agent Swarm";
}
async function tick(){
 let s=null;
 try{const r=await fetch("/events?k="+K,{cache:"no-store"});if(r.ok)s=await r.json();}catch(e){}
 if(!s){
  if(++fails>=3){
   $("clockw").className="clock lost";setHtml($("clockic"),"lost",IC.warn);
   $("clock").textContent="Disconnected";document.title="⚠ Agent Swarm";
  }
  setTimeout(tick,fails>=3?2000:400);return;
 }
 fails=0;
 if(s.started!==started){started=s.started;nWork=s.workers.length;timeout=s.timeout||0;build(s.workers);}
 s.workers.forEach(paintWorker);
 header(s);
 setTimeout(tick,400);
}
tick();
</script></body></html>"""
)


# Dedicated single-worker detail page (opened when a card is clicked / Enter): the
# same chat window as the single-run viewer (watch_ui), fed from this dashboard's
# /events by the worker's index — its prompt, live step timeline, then the answer
# or image.
_WORKER_HTML = watch_ui.page(
    "Agent Intern",
    r"""
const IDX=parseInt(Q.get("i")||"0",10);
let started=null,seen=0,fin=false,fails=0;
function rebuild(w,back){
 resetChat();seen=0;fin=false;
 if(!w){emptyState("Worker "+IDX+" is not part of the current swarm");return;}
 userBubble(w.prompt||w.label||"","Claude");
 newTrace(back==="codex");
}
function finish(w,back){
 fin=true;finishTrace(w.status);
 if(w.image)imageCard("/image?k="+K+"&p="+encodeURIComponent(w.image));
 const err=w.status==="error",meta=back+" · "+fmtS(w.elapsed);
 if(w.answer)botCard(w.answer,err?"Failed":"Answer",{copy:!err,err:err,meta:meta});
}
async function tick(){
 let s=null;
 try{const r=await fetch("/events?k="+K,{cache:"no-store"});if(r.ok)s=await r.json();}catch(e){}
 if(!s){if(++fails>=3&&!fin)setState("lost");}
 else{
  fails=0;
  const w=s.workers[IDX],back=w?bkName(w.backend):"agy";
  if(w){
   TITLE="Intern · "+(w.repo?w.repo+" · ":"")+(w.label||("Worker "+IDX));
   setAgent(w.backend,w.repo);
  }
  if(s.started!==started){started=s.started;rebuild(w,back);}
  if(!w)setState("idle");
  else{
   for(let i=seen;i<w.events.length;i++)addStep(w.events[i]);
   seen=w.events.length;
   if(w.status==="queued")setState("queued");
   else if(w.status==="working")setState("working",{start:w.started,timeout:w.timeout||s.timeout,elapsed:w.elapsed});
   else{if(!fin)finish(w,back);setState(w.status,{elapsed:w.elapsed});}
  }
 }
 setTimeout(tick,fin?1500:fails>=3?2000:400);
}
tick();
""",
)
