"""The season dashboard as a web page -- the Home Assistant add-on's sidebar panel.

Same engine, different window: the terminal app draws data/runner_status.json
with rich; this serves it to a browser. Keys become buttons and travel to the
engine through the same request file.

It also hosts the one thing a headless server cannot do by itself: signing in
to Sleeper. "Sign in" starts a real Chromium on a virtual display inside the
container and shows that display in the page (noVNC), so the password is typed
by a human into Sleeper's own page and is never seen or stored by this code --
exactly what `mish login` does on the Mac.

Everything is served with RELATIVE urls: Home Assistant's ingress mounts the
app under /api/hassio_ingress/<token>/ and only authenticated HA users get in.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import runner as rn

app = FastAPI(title="Season Runner")
NOVNC_DIR = Path(os.environ.get("NOVNC_DIR", "/usr/share/novnc"))
VNC_PORT = int(os.environ.get("VNC_PORT", "5900"))
_login: dict = {"proc": None}

if NOVNC_DIR.is_dir():
    app.mount("/novnc", StaticFiles(directory=str(NOVNC_DIR)), name="novnc")


def _status() -> dict:
    try:
        snap = json.loads(rn.STATUS_FILE.read_text())
    except (OSError, ValueError):
        snap = {"missing": True, "matchup": {}, "last": {}, "log": [], "health": {}}
    now = datetime.now()
    try:
        snap["age_s"] = (now - datetime.fromisoformat(snap["written"])).total_seconds()
    except (KeyError, ValueError):
        snap["age_s"] = None
    snap["now"] = now.strftime("%a %H:%M")
    snap["jobs"] = [{"key": j.key, "label": j.label,
                     "next": j.next_occurrence(now).strftime("%a %H:%M"),
                     "in_h": round((j.next_occurrence(now) - now).total_seconds() / 3600, 1)}
                    for j in rn.JOBS]
    snap["signed_in"] = rn.signed_in()
    proc = _login["proc"]
    snap["login_running"] = bool(proc and proc.poll() is None)
    snap["can_sign_in"] = NOVNC_DIR.is_dir()
    return snap


@app.get("/api/status")
def status():
    return JSONResponse(_status())


@app.post("/api/key/{key}")
def key(key: str):
    if key not in ("l", "w", "r"):
        return JSONResponse({"ok": False, "error": "unknown key"}, status_code=400)
    rn.REQUEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    rn.REQUEST_FILE.write_text(key)
    return {"ok": True}


@app.post("/api/login")
def login():
    """Open Sleeper in a visible browser on the virtual display."""
    proc = _login["proc"]
    if proc and proc.poll() is None:
        return {"ok": True, "already": True}
    subprocess.run(["pkill", "-f", "mishpacha-browser"], capture_output=True)
    _login["proc"] = subprocess.Popen(
        [rn.MISH, "login"], cwd=str(rn.ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":99")})
    return {"ok": True}


@app.websocket("/websockify")
async def websockify(ws: WebSocket):
    """Bridge noVNC's websocket to the local VNC server."""
    wants = ws.headers.get("sec-websocket-protocol", "")
    await ws.accept(subprotocol="binary" if "binary" in wants else None)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", VNC_PORT)
    except OSError:
        await ws.close()
        return

    async def up():
        try:
            while True:
                writer.write(await ws.receive_bytes())
                await writer.drain()
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass

    async def down():
        try:
            while data := await reader.read(65536):
                await ws.send_bytes(data)
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass

    tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in tasks:
        t.cancel()
    writer.close()
    try:
        await ws.close()
    except RuntimeError:
        pass


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Season Runner</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--ink:#16181d;--dim:#6b7280;--line:#e3e5ea;--good:#15803d;--bad:#b91c1c;--warn:#b45309;--accent:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#111318;--card:#1b1e25;--ink:#e8eaee;--dim:#9aa1ad;--line:#2b2f38;--good:#4ade80;--bad:#f87171;--warn:#fbbf24;--accent:#60a5fa}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px;display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}
header{grid-column:1/-1;display:flex;flex-wrap:wrap;gap:8px 16px;align-items:baseline}
h1{font-size:20px;margin:0}h2{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--dim);margin:0 0 10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;min-width:0}
.wide{grid-column:1/-1}.dim{color:var(--dim)}.good{color:var(--good)}.bad{color:var(--bad)}.warn{color:var(--warn)}
.score{font-size:22px;font-weight:650}.pct{font-size:30px;font-weight:700;font-variant-numeric:tabular-nums}
.bar{height:10px;border-radius:6px;background:var(--line);overflow:hidden;margin:6px 0 10px}.bar>i{display:block;height:100%}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}td,th{padding:4px 6px;text-align:left;border-bottom:1px solid var(--line)}
th{font-weight:500;color:var(--dim);font-size:12px}td.n,th.n{text-align:right}tr:last-child td{border-bottom:0}
#matchup td:nth-child(2){max-width:0;width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.card{overflow-x:auto}
button{font:inherit;padding:8px 14px;border-radius:9px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}button:disabled{opacity:.5;cursor:default}
.row{display:flex;flex-wrap:wrap;gap:8px}
pre{margin:0;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;word-break:break-word;max-height:380px;overflow:auto}
#vncwrap{display:none}iframe{width:100%;height:min(78vh,760px);border:1px solid var(--line);border-radius:10px;background:#000}
.banner{grid-column:1/-1;border-color:var(--bad)}
</style></head><body><main>
<header><h1>Season Runner</h1><span id="sub" class="dim">loading…</span></header>
<section id="signin" class="card banner" style="display:none">
  <h2>Sleeper sign-in needed</h2>
  <p>The engine cannot move players until you sign in to Sleeper once. Your password goes straight into Sleeper's own page; nothing here reads or stores it.</p>
  <div class="row"><button class="primary" onclick="signIn()">Sign in to Sleeper</button><span id="signmsg" class="dim"></span></div>
</section>
<section id="vncwrap" class="card wide"><h2>Sleeper (live browser) <span class="dim">— sign in, then wait for your team page; this closes itself</span></h2><iframe id="vnc" title="Sleeper sign-in"></iframe>
  <div class="row" style="margin-top:8px"><button onclick="closeVnc()">Close</button></div></section>
<section class="card"><h2>This week</h2><div id="matchup" class="dim">loading…</div></section>
<section class="card"><h2>Automation</h2><table id="jobs"></table><p id="health" class="dim" style="margin:10px 0 12px"></p>
  <div class="row"><button onclick="key('l')">Set lineup now</button><button onclick="key('w')">Preview waiver move</button><button onclick="key('r')">Refresh</button></div></section>
<section class="card wide"><h2>Activity</h2><pre id="log"></pre></section>
</main><script>
const $=id=>document.getElementById(id), esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
let vncOpen=false;
async function key(k){await fetch('api/key/'+k,{method:'POST'});tick()}
async function signIn(){$('signmsg').textContent='opening Sleeper…';await fetch('api/login',{method:'POST'});
  const path=(location.pathname.replace(/[^/]*$/,'')+'websockify').replace(/^\//,'');
  setTimeout(()=>{$('vnc').src='novnc/vnc.html?autoconnect=1&resize=scale&reconnect=1&path='+encodeURIComponent(path);$('vncwrap').style.display='block';vncOpen=true;$('signmsg').textContent=''},2500)}
function closeVnc(){$('vnc').src='about:blank';$('vncwrap').style.display='none';vncOpen=false}
function render(s){
  const m=s.matchup||{},stale=s.missing||(s.age_s!=null&&s.age_s>180);
  const wk=m.week??'?',aw=m.active_week;
  $('sub').innerHTML=(aw&&aw!==wk?`week ${wk} final · playing for week ${aw}`:`week ${wk}`)+' · '+
    (stale?'<span class="bad">engine not responding</span>':s.running?`<span class="warn">running: ${esc(s.running)}</span>`:'<span class="good">idle — waiting for next job</span>')+` · ${esc(s.now||'')}`;
  $('signin').style.display=(!s.signed_in&&s.can_sign_in)?'block':'none';
  if(s.signed_in&&vncOpen&&!s.login_running)closeVnc();
  if(m.mine&&m.theirs&&m.win!=null){const w=m.win,c=w>=.6?'good':w<=.4?'bad':'warn';
    let h=`<div class="score"><span class="${m.me_pts>=m.opp_pts?'good':'bad'}">You ${m.me_pts.toFixed(2)}</span> <span class="dim">vs</span> ${esc(m.opp_name)} ${m.opp_pts.toFixed(2)}</div>
    <div class="pct ${c}">${Math.round(w*100)}% <span style="font-size:14px;font-weight:500">to win</span></div>
    <div class="bar"><i style="width:${w*100}%;background:var(--${c})"></i></div>
    <div class="dim">projected <b style="color:var(--ink)">${m.mine.expected.toFixed(1)}</b> (±${m.mine.sd.toFixed(0)}) – ${m.theirs.expected.toFixed(1)} (±${m.theirs.sd.toFixed(0)})</div>
    <table style="margin-top:10px"><tr><th></th><th>starter</th><th class="n">pts</th><th class="n">proj</th><th></th></tr>`;
    for(const l of m.mine.lines){const live=l.state==='in';
      h+=`<tr><td class="dim">${esc(l.slot)}</td><td>${esc(l.name)}</td><td class="n">${l.state==='post'||live?l.actual.toFixed(1):'–'}</td><td class="n">${l.expected.toFixed(1)}</td><td class="${live?'good':'dim'}">${({post:'final',in:'live',bye:'no game'})[l.state]||''}</td></tr>`}
    $('matchup').className='';$('matchup').innerHTML=h+`</table><p class="dim" style="margin:8px 0 0">updated ${esc(m.updated||'')}</p>`}
  else $('matchup').textContent=m.error||'loading matchup…';
  let j='<tr><th>Job</th><th>Next run</th><th>Last result</th></tr>';
  for(const job of s.jobs||[]){const l=(s.last||{})[job.key];
    const eta=job.in_h>=24?`${Math.floor(job.in_h/24)}d ${Math.round(job.in_h%24)}h`:`${job.in_h}h`;
    j+=`<tr><td>${esc(job.label)}</td><td>${esc(job.next)} <span class="dim">(in ${eta})</span></td><td>${l?`<span class="${l.result.startsWith('ok')?'good':'bad'}">${esc(l.result)}</span> <span class="dim">${esc(l.at.replace('T',' ').slice(5,16))} — ${esc(l.summary||'')}</span>`:'<span class="dim">not run yet</span>'}</td></tr>`}
  $('jobs').innerHTML=j;
  const h=s.health||{};
  $('health').innerHTML=`browser automation ${h.executor?'<span class="good">ready</span>':'<span class="bad">missing</span>'} · Sleeper ${s.signed_in?'<span class="good">signed in</span>':'<span class="bad">not signed in</span>'}`;
  const pre=$('log'),atEnd=pre.scrollTop+pre.clientHeight>=pre.scrollHeight-30;
  pre.textContent=(s.log||[]).map(x=>x[0]+'  '+x[2]).join('\n');if(atEnd)pre.scrollTop=pre.scrollHeight}
async function tick(){try{render(await (await fetch('api/status')).json())}catch(e){$('sub').innerHTML='<span class="bad">cannot reach the add-on</span>'}}
tick();setInterval(tick,3000);
</script></body></html>
"""
