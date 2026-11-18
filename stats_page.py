# ruff: noqa: E501  (the page is one embedded HTML/JS string)
"""The /stats page of the local watch server: one inline script, inline SVG, no outside loads.

It is built on watch_ui's BASE_CSS and BASE_JS (palette, fonts, backend logos), so it looks
like the run and swarm windows. The numbers come from /stats.json on the same origin (see
stats_tools.stats_payload), asked again on every range switch, and agy's quota once from
/stats/quota.json, because reading it can take seconds. Every value reaches the DOM through
textContent or an SVG attribute set from a number, never as HTML, so nothing in the
statistics file can inject markup.
"""

from __future__ import annotations

import watch_ui

_PAGE = r"""<!doctype html><html lang="en" translate="no"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="google" content="notranslate">
<title>agent-intern stats</title>
<style>
__BASE_CSS__
.wrap{max-width:1120px;margin:0 auto;padding:22px 24px 30px}
.top{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:16px}
.top h1{font:650 18px/1.25 var(--sans);margin:0;letter-spacing:-.01em}
.top .sub{color:var(--tx3);font-size:12px;margin-top:2px}
.tright{margin-left:auto;display:flex;align-items:center;gap:12px}
.upd{color:var(--tx3);font-size:11.5px;display:flex;align-items:center;gap:7px;white-space:nowrap}
.upd .ring{width:10px;height:10px;color:var(--tx3)}
.seg{display:inline-flex;background:var(--s1);border:1px solid var(--bd);border-radius:10px;padding:3px;gap:2px}
.seg button{appearance:none;border:0;background:transparent;cursor:pointer;font:600 12px/1 var(--sans);color:var(--tx2);padding:7px 13px;border-radius:7px}
.seg button:hover{color:var(--tx);background:var(--s2)}
.seg button[aria-pressed=true]{background:var(--s3);color:var(--tx);box-shadow:inset 0 0 0 1px var(--bd)}
.seg button:focus-visible{outline:2px solid var(--cyan);outline-offset:1px}
.note{display:flex;gap:8px;align-items:center;margin:0 0 12px;padding:9px 12px;border-radius:10px;color:#f8d48f;background:rgba(245,185,74,.08);border:1px solid rgba(245,185,74,.25);font-size:12.5px}
.card{background:linear-gradient(180deg,var(--s1),#0d1016);border:1px solid var(--bd2);border-radius:14px;padding:15px 16px;min-width:0}
.hd{display:flex;align-items:baseline;gap:10px;margin-bottom:12px;flex-wrap:wrap}
.hd h2{font:600 13.5px/1.3 var(--sans);margin:0}
.hd .aside{color:var(--tx3);font-size:12px}
.legend{margin-left:auto;display:flex;gap:14px;color:var(--tx2);font-size:12px}
.legend i{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:12px}
.kpi .lbl{color:var(--tx3);font:600 11px/1.3 var(--sans);text-transform:uppercase;letter-spacing:.07em}
.kpi .val{font:650 27px/1.15 var(--sans);margin-top:7px;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.kpi .val.bad{color:var(--red)}
.kpi .foot{color:var(--tx2);font-size:12px;margin-top:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chartbox{position:relative}
.chart .gl{stroke:#1d222d;stroke-width:1}
.chart .ax{fill:var(--tx3);font:10.5px var(--sans)}
.chart .ok{fill:var(--cyan)}
.chart .er{fill:var(--red)}
.chart .hit{fill:transparent}
.chart .col:hover .hit{fill:rgba(255,255,255,.035)}
.tip{position:absolute;pointer-events:none;background:var(--s3);border:1px solid var(--bd);border-radius:9px;padding:7px 10px;font-size:12px;white-space:nowrap;box-shadow:0 8px 24px rgba(0,0,0,.45);transform:translate(-50%,-100%);margin-top:-8px}
.tip b{display:block;font-weight:600;margin-bottom:2px}
.tip span{color:var(--tx2)}
.mt{margin-top:12px}
.grid2{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px;margin-top:12px}
.tw{overflow-x:auto}
table{width:100%;border-collapse:collapse}
th{font:600 10.5px/1 var(--sans);color:var(--tx3);text-transform:uppercase;letter-spacing:.06em;text-align:left;padding:0 10px 9px;white-space:nowrap}
td{padding:9px 10px;border-top:1px solid var(--bd2);vertical-align:middle;font-variant-numeric:tabular-nums;white-space:nowrap}
th.n,td.n{text-align:right}
.bname{display:flex;align-items:center;gap:10px;font-weight:600}
.rate{display:flex;align-items:center;gap:9px;min-width:130px}
.rate .pc{width:38px;text-align:right;color:var(--tx2)}
.meter{flex:1;height:6px;border-radius:4px;background:var(--s3);overflow:hidden;display:flex}
.meter b{display:block;height:100%}
.pill{display:inline-block;font:600 11px/1.7 var(--sans);padding:0 7px;border-radius:6px;color:#ffb4b4;background:rgba(255,107,107,.09);border:1px solid rgba(255,107,107,.24)}
.mut{color:var(--tx3)}
.small{font-size:11.5px}
.empty{color:var(--tx3);padding:22px 6px;text-align:center}
.sw{display:flex;align-items:center;gap:12px;padding:10px 0;border-top:1px solid var(--bd2)}
.sw:first-child{border-top:0;padding-top:2px}
.sw .main{flex:1;min-width:0}
.sw .t{font-weight:600}
.sw .s{color:var(--tx3);font-size:12px;margin-top:1px}
.sw .cnt{font-variant-numeric:tabular-nums;color:var(--tx2);white-space:nowrap}
.path{display:flex;align-items:center;gap:6px;margin-top:5px;min-width:0}
.path code{font:500 11px/1.7 var(--mono);color:var(--tx2);background:var(--s2);border:1px solid var(--bd2);border-radius:6px;padding:0 6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.cp{appearance:none;cursor:pointer;font:600 11px/1.6 var(--sans);color:var(--tx2);background:transparent;border:1px solid var(--bd);border-radius:6px;padding:0 7px}
.cp:hover{color:var(--tx)}.cp.ok{color:var(--green);border-color:rgba(63,223,127,.35)}
.qf{margin-top:14px}.qf:first-child{margin-top:0}
.qf .fam{font-weight:600;margin-bottom:6px}
.ql{display:grid;grid-template-columns:minmax(70px,auto) 1fr 44px;gap:10px;align-items:center;font-size:12.5px;margin-top:6px}
.ql .nm{color:var(--tx2)}
.ql .pc{text-align:right;font-variant-numeric:tabular-nums}
.ql .rs{grid-column:2/4;color:var(--tx3);font-size:11.5px;margin-top:-3px}
.foot{color:var(--tx3);font-size:11.5px;margin-top:16px;line-height:1.6}
main{transition:opacity .15s}main.busy{opacity:.5}
@media (max-width:860px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.grid2{grid-template-columns:minmax(0,1fr)}}
@media (max-width:560px){.wrap{padding:14px 12px 22px}.tright{margin-left:0;width:100%;justify-content:space-between}.kpi .val{font-size:22px}.hide-sm{display:none}}
</style></head><body><div class="wrap">
<div class="top">
 <div><h1>agent-intern stats</h1><div class="sub">What you delegated, from a local file. Numbers only: no prompts or answers are stored.</div></div>
 <div class="tright"><span class="upd" id="upd"></span><div class="seg" id="seg" role="group" aria-label="Range"></div></div>
</div>
<div id="note"></div>
<main id="main">
 <div class="kpis" id="kpis"></div>
 <section class="card"><div class="hd"><h2 id="charttitle">Calls</h2><span class="aside" id="chartaside"></span>
  <div class="legend"><span><i style="background:var(--cyan)"></i>OK</span><span><i style="background:var(--red)"></i>Failed</span></div></div>
  <div class="chartbox" id="chartbox"><div id="chart" class="chart"></div></div></section>
 <section class="card mt"><div class="hd"><h2>Backends</h2><span class="aside">success counts a refused call (a bad argument) as the caller's, not the backend's</span></div>
  <div class="tw" id="backends"></div></section>
 <div class="grid2">
  <section class="card"><div class="hd"><h2>Recent swarms</h2><span class="aside" id="swaside"></span></div><div id="swarms"></div></section>
  <section class="card"><div class="hd"><h2>Antigravity quota</h2><span class="aside">read from agy, spends none</span></div><div id="quota"></div></section>
 </div>
</main>
<p class="foot">Times are this computer's local time. Tokens show only where a backend reports them (Antigravity today). Error categories are best effort. Turn recording off with AGENT_INTERN_NO_STATS=1.</p>
</div>
<script>
__BASE_JS__
const RANGES=[[1,"24h"],[7,"7d"],[30,"30d"],[90,"90d"]];
let DAYS=Number(Q.get("days"));if(!RANGES.some(r=>r[0]===DAYS))DAYS=7;
let LAST=null,SEQ=0;
const NS="http://www.w3.org/2000/svg";
const KIND={preset:"Preset swarm",swarm:"Agent swarm",image:"Image swarm"};
const CAT={quota:"quota",auth:"not signed in",timeout:"timeout",not_installed:"not installed",cancelled:"cancelled",nesting:"nested call",exit_error:"exit error",error:"error",partial:"partial",other:"other"};
function sv(tag,attrs,parent){const e=document.createElementNS(NS,tag);for(const k in attrs)e.setAttribute(k,String(attrs[k]));if(parent)parent.appendChild(e);return e;}
function compact(n){n=+n||0;if(n>=1e6)return (n/1e6).toFixed(n>=1e7?0:1)+"M";if(n>=1e4)return Math.round(n/1e3)+"k";if(n>=1e3)return (n/1e3).toFixed(1)+"k";return String(n);}
function plural(n,w){return n+" "+w+(n===1?"":"s");}
function dt(ts,o){return new Date(ts*1000).toLocaleString(undefined,o);}
function when(ts){return dt(ts,{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"});}
function bucketLabel(ts,unit){return unit==="hour"?dt(ts,{hour:"2-digit",minute:"2-digit"}):dt(ts,{month:"short",day:"numeric"});}
function bucketTitle(ts,unit){
 if(unit==="hour")return dt(ts,{weekday:"short",hour:"2-digit",minute:"2-digit"});
 if(unit==="week")return "Week of "+dt(ts,{month:"short",day:"numeric"});
 return dt(ts,{weekday:"short",month:"short",day:"numeric"});}
function until(iso){const t=Date.parse(iso);if(!t)return "";let s=Math.round((t-Date.now())/1000);if(s<=0)return "resets now";
 const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
 return "resets in "+(d?d+"d "+h+"h":h?h+"h "+m+"m":m+"m");}
function niceStep(v){const p=Math.pow(10,Math.floor(Math.log10(v)));const f=v/p;return (f<=1?1:f<=2?2:f<=5?5:10)*p;}
function logo(b){if(BK[b]){return avatar(b);}const a=el("div","av",(b||"?").slice(0,2));a.title=b;return a;}
function prettyName(b){return NAMES[BK[b]]||b;}

function segment(){const s=$("seg");s.textContent="";
 RANGES.forEach(([n,label])=>{const b=el("button",null,label);b.type="button";b.setAttribute("aria-pressed",String(n===DAYS));
  b.title=n===1?"Last 24 hours, per hour":n<=31?"Last "+n+" days, per day":"Last "+n+" days, per week";
  b.onclick=()=>{if(n===DAYS)return;DAYS=n;segment();load();};s.appendChild(b);});}

function kpi(label,val,foot,bad){const c=el("div","card kpi");c.appendChild(el("div","lbl",label));
 c.appendChild(el("div","val"+(bad?" bad":""),val));c.appendChild(el("div","foot",foot));return c;}
function renderKpis(t){const k=$("kpis");k.textContent="";
 const n=t.calls,rate=n?100-t.error_rate:null,tok=t.tokens||{},tin=tok.i||0,tout=tok.o||0;
 k.appendChild(kpi("Delegated calls",String(n),t.swarms?"incl. "+plural(t.swarms,"swarm run"):"no swarm runs"));
 k.appendChild(kpi("Success rate",rate===null?"—":rate+"%",n?plural(t.errors,"failed call")+(t.refused?", "+t.refused+" refused":""):"nothing to rate yet",rate!==null&&rate<80));
 k.appendChild(kpi("Average time",n?fmtS(t.avg_s):"—","per call, start to answer"));
 k.appendChild(kpi("Tokens",tin+tout?compact(tin+tout):"—",tin+tout?compact(tin)+" in · "+compact(tout)+" out"+(tok.c?" · "+compact(tok.c)+" cached":""):"no backend reported tokens"));}

function renderChart(series){
 const box=$("chart");box.textContent="";const tip=$("tip");if(tip)tip.remove();
 const unit=series.unit,B=series.buckets||[];
 $("charttitle").textContent=unit==="hour"?"Calls per hour":unit==="week"?"Calls per week":"Calls per day";
 const total=B.reduce((a,b)=>a+b.calls,0);
 $("chartaside").textContent=unit==="hour"?"last 24 hours":"last "+DAYS+" days";
 if(!total){box.appendChild(el("div","empty","No delegated calls in this range. Calls you delegate through agent-intern show up here."));return;}
 const W=Math.max(280,box.clientWidth||600),H=220,L=34,R=4,T=10,BM=26,ih=H-T-BM,iw=W-L-R;
 const peak=Math.max(1,...B.map(b=>b.calls)),step=niceStep(Math.max(1,peak/4)),max=Math.max(step*Math.ceil(peak/step),step);
 const s=sv("svg",{viewBox:"0 0 "+W+" "+H,width:W,height:H,role:"img","aria-label":$("charttitle").textContent+": OK and failed calls"},box);
 for(let v=0;v<=max+1e-9;v+=step){const y=T+ih-ih*v/max;
  sv("line",{class:"gl",x1:L,x2:W-R,y1:y,y2:y},s);
  const t=sv("text",{class:"ax",x:L-8,y:y+3.5,"text-anchor":"end"},s);t.textContent=String(Math.round(v));}
 const colw=iw/B.length,bw=Math.max(2,Math.min(34,colw*0.62)),every=Math.max(1,Math.ceil(B.length/Math.max(2,Math.floor(iw/62))));
 const tipEl=el("div","tip");tipEl.id="tip";tipEl.hidden=true;$("chartbox").appendChild(tipEl);
 B.forEach((b,i)=>{const g=sv("g",{class:"col"},s),x=L+colw*i,bx=x+(colw-bw)/2;
  sv("rect",{class:"hit",x:x,y:T,width:colw,height:ih},g);
  const hok=ih*b.ok/max,her=ih*b.err/max;
  if(b.ok)sv("rect",{class:"ok",x:bx,y:T+ih-hok,width:bw,height:Math.max(1,hok),rx:Math.min(3,bw/3)},g);
  if(b.err)sv("rect",{class:"er",x:bx,y:T+ih-hok-her,width:bw,height:Math.max(1.5,her),rx:Math.min(3,bw/3)},g);
  if(i%every===0||i===B.length-1&&B.length<=8){const t=sv("text",{class:"ax",x:x+colw/2,y:H-8,"text-anchor":"middle"},s);t.textContent=bucketLabel(b.start,unit);}
  g.addEventListener("mouseenter",()=>{tipEl.textContent="";tipEl.appendChild(el("b",null,bucketTitle(b.start,unit)));
   tipEl.appendChild(el("span",null,b.calls?plural(b.calls,"call")+" · "+b.ok+" OK"+(b.err?" · "+b.err+" failed":""):"no calls"));
   tipEl.style.left=(x+colw/2)+"px";tipEl.style.top=(T+ih-hok-her)+"px";tipEl.hidden=false;});
  g.addEventListener("mouseleave",()=>{tipEl.hidden=true;});});}

function renderBackends(bk){const box=$("backends");box.textContent="";
 const rows=Object.entries(bk).filter(([n])=>n!=="swarm").sort((a,b)=>b[1].calls-a[1].calls);
 if(!rows.length){box.appendChild(el("div","empty","No backend calls in this range."));return;}
 const t=el("table"),h=el("tr");
 [["Backend",""],["Calls","n"],["Success",""],["Avg time","n"],["Tokens in / out","n hide-sm"],["Last failure","hide-sm"]].forEach(([x,c])=>h.appendChild(el("th",c||null,x)));
 t.appendChild(h);
 rows.forEach(([n,b])=>{const tr=el("tr");
  const c0=el("td"),nm=el("div","bname");nm.appendChild(logo(n));nm.appendChild(el("span",null,prettyName(n)));c0.appendChild(nm);tr.appendChild(c0);
  tr.appendChild(el("td","n",String(b.calls)));
  const c2=el("td"),r=el("div","rate"),m=el("div","meter"),refused=b.refused||0,failed=b.err-refused;
  [[b.ok,"var(--cyan)"],[failed,"var(--red)"],[refused,"var(--tx3)"]].forEach(([v,col])=>{if(v){const x=el("b");x.style.width=(100*v/b.calls)+"%";x.style.background=col;m.appendChild(x);}});
  r.title=b.ok+" OK · "+failed+" failed"+(refused?" · "+refused+" refused":"");
  r.appendChild(m);r.appendChild(el("span","pc",(100-b.error_rate)+"%"));c2.appendChild(r);tr.appendChild(c2);
  tr.appendChild(el("td","n",fmtS(b.avg_s)));
  const tk=b.tokens||{};tr.appendChild(el("td","n hide-sm"+(tk.i||tk.o?"":" mut"),tk.i||tk.o?compact(tk.i||0)+" / "+compact(tk.o||0):"not reported"));
  const c5=el("td","hide-sm");
  if(b.last_err){c5.appendChild(el("span","pill",CAT[b.last_err.cat]||b.last_err.cat));c5.appendChild(el("span","mut small","  "+when(b.last_err.ts)));}
  else c5.appendChild(el("span","mut","none"));
  tr.appendChild(c5);t.appendChild(tr);});
 box.appendChild(t);}

function renderSwarms(list,total){const box=$("swarms");box.textContent="";
 $("swaside").textContent=total?plural(total,"run")+" in range":"";
 if(!list.length){box.appendChild(el("div","empty","No swarms in this range."));return;}
 list.forEach(r=>{const row=el("div","sw"),mn=el("div","main");
  mn.appendChild(el("div","t",KIND[r.kind]||r.kind));mn.appendChild(el("div","s",when(r.ts)+" · "+fmtS((r.ms||0)/1000)));
  if(r.run){const p=el("div","path"),txt="~/.agent-intern/runs/"+r.run,c=el("code",null,txt),b=el("button","cp","Copy");
   c.title="Long answers saved here";b.type="button";b.onclick=()=>copyText(txt,b);p.appendChild(c);p.appendChild(b);mn.appendChild(p);}
  row.appendChild(mn);
  const ok=r.workers_ok,all=r.workers,cnt=el("div","cnt",(ok===undefined?"?":ok)+" / "+(all===undefined?"?":all)+" OK");
  if(all!==undefined&&ok!==all)cnt.style.color="var(--amber)";row.appendChild(cnt);box.appendChild(row);});}

function renderQuota(q){const box=$("quota");box.textContent="";
 if(q===undefined){const w=el("div","upd");w.appendChild(el("span","ring"));w.appendChild(el("span",null,"Reading agy’s quota…"));box.appendChild(w);return;}
 if(!q||!q.length){box.appendChild(el("div","empty","Not available: Antigravity is not enabled here, or this agy cannot report its quota."));return;}
 q.forEach(f=>{const fam=el("div","qf");fam.appendChild(el("div","fam",f.family));
  f.limits.forEach(l=>{const row=el("div","ql"),m=el("div","meter"),b=el("b"),v=l.left;
   row.appendChild(el("span","nm",l.name));
   if(typeof v==="number"){b.style.width=Math.max(0,Math.min(100,v))+"%";b.style.background=v>50?"var(--green)":v>20?"var(--amber)":"var(--red)";m.appendChild(b);}
   row.appendChild(m);row.appendChild(el("span","pc",l.text));
   if(l.resets){row.appendChild(el("span","rs",until(l.resets)));}
   fam.appendChild(row);});
  box.appendChild(fam);});}

function render(d){LAST=d;
 const note=$("note");note.textContent="";if(d.recording)note.appendChild(el("div","note","Note: "+d.recording));
 const s=d.summary;renderKpis(s.totals);renderChart(s.series||{unit:"day",buckets:[]});
 renderBackends(s.backends);renderSwarms(s.swarms,s.totals.swarms);}

function stamp(busy){const u=$("upd");u.textContent="";
 if(busy){u.appendChild(el("span","ring"));u.appendChild(el("span",null,"Loading…"));return;}
 u.appendChild(el("span",null,"Updated "+new Date().toLocaleTimeString(undefined,{hour:"2-digit",minute:"2-digit"})));}

async function load(){const my=++SEQ;$("main").classList.add("busy");stamp(true);
 try{history.replaceState(null,"","?days="+DAYS+"&k="+K);}catch(e){}
 try{const r=await fetch("/stats.json?days="+DAYS+"&k="+K,{cache:"no-store"});
  if(!r.ok)throw new Error("HTTP "+r.status);const d=await r.json();if(my!==SEQ)return;render(d);stamp(false);}
 catch(e){if(my!==SEQ)return;const u=$("upd");u.textContent="Could not load the statistics ("+e.message+")";}
 finally{if(my===SEQ)$("main").classList.remove("busy");}}

async function loadQuota(){renderQuota(undefined);
 try{const r=await fetch("/stats/quota.json?k="+K,{cache:"no-store"});if(!r.ok)throw new Error();renderQuota((await r.json()).quota);}
 catch(e){renderQuota(null);}}

let RZ=0;addEventListener("resize",()=>{clearTimeout(RZ);RZ=setTimeout(()=>{if(LAST)renderChart(LAST.summary.series||{unit:"day",buckets:[]});},120);});
setInterval(()=>{if(!document.hidden)load();},60000);
segment();load();loadQuota();
</script></body></html>
"""

PAGE = _PAGE.replace("__BASE_CSS__", watch_ui.BASE_CSS).replace("__BASE_JS__", watch_ui.BASE_JS)
