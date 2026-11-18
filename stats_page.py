# ruff: noqa: E501  (the page is one embedded HTML/JS string)
"""The /stats page of the local watch server: one inline script, inline SVG, no outside loads.

The data comes from /stats.json on the same origin (see stats_tools.stats_payload). Every
value reaches the DOM through textContent or an SVG attribute set from a number, never as
HTML, so nothing in the statistics file can inject markup.
"""

from __future__ import annotations

PAGE = r"""<!doctype html><html lang="en" translate="no"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="google" content="notranslate">
<title>agent-intern stats</title>
<style>
:root{color-scheme:dark;--bg:#0a1230;--card:#101b45;--line:#33478c;--fg:#e8edff;--mut:#8d9bc8;
--ok:#5b8cff;--err:#ff7a8a}
*{box-sizing:border-box}body{margin:0;padding:20px;background:var(--bg);color:var(--fg);
font:14px/1.45 "Segoe UI",system-ui,sans-serif}
h1{font-size:20px;margin:0 0 4px}h2{font-size:14px;margin:22px 0 8px;color:var(--mut);
text-transform:uppercase;letter-spacing:.08em}
.sub{color:var(--mut);margin:0 0 10px}.note{background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:8px 12px;margin:8px 0}
table{border-collapse:collapse;width:100%;max-width:820px}th,td{padding:6px 10px;
border-bottom:1px solid #1d2b66;text-align:left}th{color:var(--mut);font-weight:600}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.bad{color:var(--err)}svg{max-width:820px;width:100%;height:auto;display:block}
.tabs button{background:var(--card);color:var(--fg);border:1px solid var(--line);
border-radius:8px;padding:4px 10px;margin-right:6px;cursor:pointer}
.tabs button[aria-pressed=true]{border-color:var(--ok);color:#fff}
pre{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px;
overflow:auto;max-width:820px}
</style></head><body>
<h1>agent-intern stats</h1>
<p class="sub" id="sub">loading…</p>
<div class="tabs" id="tabs"></div>
<div id="notes"></div>
<h2>Calls per day</h2><div id="chart"></div>
<h2>Backends</h2><div id="backends"></div>
<h2>Backend health</h2><div id="health"></div>
<h2>Recent swarms</h2><div id="swarms"></div>
<div id="quotawrap"></div>
<p class="sub" id="foot"></p>
<script>
const K=new URLSearchParams(location.search).get("k")||"";
let DAYS=Number(new URLSearchParams(location.search).get("days"))||7;
const $=id=>document.getElementById(id);
const NS="http://www.w3.org/2000/svg";
function el(tag,text,cls){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;
 if(cls)e.className=cls;return e;}
function table(head,rows,numFrom){const t=el("table"),h=el("tr");
 head.forEach((x,i)=>h.appendChild(el("th",x,i>=numFrom?"n":"")));t.appendChild(h);
 rows.forEach(r=>{const tr=el("tr");r.forEach((c,i)=>tr.appendChild(el("td",String(c),
  (i>=numFrom?"n":"")+(c&&c.bad?" bad":""))));t.appendChild(tr);});return t;}
function when(ts){return new Date(ts*1000).toISOString().replace("T"," ").slice(0,16)+" UTC";}
function tok(t){const k=Object.keys(t||{});return k.length?["i","o","c"].map(x=>(t[x]||0)
 .toLocaleString("en-US")).join(" / "):"n/a";}
function bars(days){const keys=Object.keys(days);if(!keys.length)return el("p","No calls yet.","sub");
 const W=820,H=200,pad=28,bw=Math.max(6,Math.min(48,(W-pad*2)/keys.length-6));
 const max=Math.max(1,...keys.map(k=>days[k].calls));
 const s=document.createElementNS(NS,"svg");s.setAttribute("viewBox","0 0 "+W+" "+H);
 s.setAttribute("role","img");s.setAttribute("aria-label","Calls per day, ok and errors");
 keys.forEach((k,i)=>{const d=days[k],x=pad+i*(bw+6),h1=(H-50)*d.ok/max,h2=(H-50)*d.err/max;
  const a=document.createElementNS(NS,"rect");a.setAttribute("x",x);a.setAttribute("width",bw);
  a.setAttribute("y",H-30-h1);a.setAttribute("height",h1);a.setAttribute("fill","#5b8cff");
  a.setAttribute("rx",3);s.appendChild(a);
  const b=document.createElementNS(NS,"rect");b.setAttribute("x",x);b.setAttribute("width",bw);
  b.setAttribute("y",H-30-h1-h2);b.setAttribute("height",h2);b.setAttribute("fill","#ff7a8a");
  b.setAttribute("rx",3);s.appendChild(b);
  const t=document.createElementNS(NS,"text");t.setAttribute("x",x+bw/2);t.setAttribute("y",H-12);
  t.setAttribute("text-anchor","middle");t.setAttribute("fill","#8d9bc8");
  t.setAttribute("font-size","11");t.textContent=k.slice(5);s.appendChild(t);
  const n=document.createElementNS(NS,"text");n.setAttribute("x",x+bw/2);
  n.setAttribute("y",Math.max(12,H-34-h1-h2));n.setAttribute("text-anchor","middle");
  n.setAttribute("fill","#e8edff");n.setAttribute("font-size","11");n.textContent=String(d.calls);
  s.appendChild(n);});
 return s;}
function render(d){
 $("sub").textContent="Last "+d.days+" day(s). Numbers only; no prompts or answers are stored.";
 const tabs=$("tabs");tabs.textContent="";
 [1,7,30,90].forEach(n=>{const b=el("button",n+"d");b.setAttribute("aria-pressed",String(n===d.days));
  b.onclick=()=>{DAYS=n;load();};tabs.appendChild(b);});
 const notes=$("notes");notes.textContent="";
 if(d.recording)notes.appendChild(el("div","Note: "+d.recording,"note"));
 const s=d.summary;
 $("chart").textContent="";$("chart").appendChild(bars(s.days));
 const bk=Object.entries(s.backends);
 $("backends").textContent="";
 $("backends").appendChild(bk.length?table(["backend","calls","ok","errors","avg s","tokens in/out/cache"],
  bk.map(([n,b])=>[n,b.calls,b.ok,b.err,b.avg_s,tok(b.tokens)]),1):el("p","No delegated calls in this period.","sub"));
 $("health").textContent="";
 $("health").appendChild(bk.length?table(["backend","error rate","last error"],
  bk.map(([n,b])=>[n,b.error_rate+"%",b.last_err?b.last_err.cat+" ("+when(b.last_err.ts)+")":"none"]),3):el("p","-","sub"));
 $("swarms").textContent="";
 $("swarms").appendChild(s.swarms.length?table(["when","kind","tasks","ok","saved answers"],
  s.swarms.map(r=>[when(r.ts),r.kind,r.workers===undefined?"?":r.workers,r.workers_ok===undefined?"?":r.workers_ok,
   r.run?"~/.agent-intern/runs/"+r.run:"none (all fit in the reply)"]),2):el("p","No swarms in this period.","sub"));
 const q=$("quotawrap");q.textContent="";
 if(d.quota){q.appendChild(el("h2","Antigravity quota left"));q.appendChild(el("pre",d.quota));}
 $("foot").textContent="Tokens show only where a backend reports them. Error categories are best effort.";
}
async function load(){
 try{const r=await fetch("/stats.json?days="+DAYS+"&k="+encodeURIComponent(K),{cache:"no-store"});
  if(!r.ok)throw new Error(r.status);render(await r.json());}
 catch(e){$("sub").textContent="Could not load the statistics ("+e.message+").";}
}
load();
</script></body></html>
"""
