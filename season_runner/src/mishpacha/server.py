"""Live draft dashboard.

One page, open on a phone during the draft. It polls Sleeper, keeps the board
in sync, and shows the engine's recommendation the moment the clock reaches us.

The engine is warmed at startup so the first pick does not pay the board-build
cost, and recommendations are computed once per pick (the board cannot change
while we are on the clock) and cached.
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from . import config as cfg
from .data import sleeper
from .draft.recommend import DraftBrain
from .live import LiveDraft
from .model.board import build_board


@dataclass
class Snapshot:
    ready: bool = False
    error: str | None = None
    status: str = "starting"
    my_slot: int = cfg.MY_DRAFT_SLOT
    picks_made: int = 0
    on_clock: int = 0
    on_clock_who: str = ""
    is_my_turn: bool = False
    synced: bool = True
    pick: dict | None = None
    alternates: list = field(default_factory=list)
    reasoning: list = field(default_factory=list)
    survival: dict = field(default_factory=dict)
    roster: list = field(default_factory=list)
    feed: list = field(default_factory=list)
    queue: list = field(default_factory=list)
    needs: dict = field(default_factory=dict)
    endgame: bool = False
    updated: float = 0.0


class Engine:
    def __init__(self, poll: float = 2.0, sims: int = 500):
        self.snap = Snapshot()
        self.poll = poll
        self.sims = sims
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.brain: DraftBrain | None = None
        self.ld: LiveDraft | None = None

    # -- setup ----------------------------------------------------------
    def warm(self) -> None:
        board, _ = build_board()
        self.brain = DraftBrain(board, my_slot=cfg.MY_DRAFT_SLOT)
        self.brain.n_sims = self.sims
        self.ld = LiveDraft(brain=self.brain, poll_seconds=self.poll)
        slot = self.ld.resolve_slot()
        if slot:
            self.ld.my_slot = slot
            self.ld.state.my_slot = slot
            self.brain.my_slot = slot
        with self._lock:
            self.snap.my_slot = self.ld.my_slot
            self.snap.ready = True
            self.snap.status = "watching"

    # -- loop -----------------------------------------------------------
    def run(self) -> None:
        try:
            self.warm()
        except Exception as exc:
            with self._lock:
                self.snap.error = f"{type(exc).__name__}: {exc}"
                self.snap.status = "failed to start"
            return

        assert self.ld and self.brain
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:
                with self._lock:
                    self.snap.error = f"{type(exc).__name__}: {exc}"
            self._stop.wait(self.poll)

    def _tick(self) -> None:
        assert self.ld and self.brain
        _new, rec = self.ld.poll_once()
        st = self.ld.state
        ent = self.brain.entries

        who = next((m.label for m in cfg.DRAFT_ORDER if m.slot == st.on_the_clock_slot),
                   f"slot {st.on_the_clock_slot}")

        # Live queue: rebuilt every poll, so it already excludes everyone gone
        # and is shaped by what our roster still needs. Read it top-down --
        # the highest name still on the board is the pick.
        from .draft.queueing import QUEUE_CAPS
        counts: dict[str, int] = {}
        for pid in st.me.players:
            pos = self.ld.positions.get(pid, "?")
            counts[pos] = counts.get(pos, 0) + 1
        needs = {pos: n - counts.get(pos, 0) for pos, n in cfg.STARTER_SLOTS.items()
                 if n - counts.get(pos, 0) > 0}
        left = cfg.DRAFT_ROUNDS - len(st.me.players)
        endgame = left <= sum(needs.values()) + 2

        avail = [e for e in self.brain.board if e.player_id not in st.drafted]
        avail.sort(key=lambda e: -e.vor)
        picked: dict[str, int] = {}
        queue = []
        for e in avail:
            if not endgame and e.position in ("K", "DEF"):
                continue
            have = counts.get(e.position, 0) + picked.get(e.position, 0)
            if have >= QUEUE_CAPS.get(e.position, 99):
                continue
            picked[e.position] = picked.get(e.position, 0) + 1
            queue.append({"n": len(queue) + 1, "name": e.name, "pos": e.position,
                          "team": e.team or "", "adp": round(e.adp, 1),
                          "bye": e.bye, "need": e.position in needs})
            if len(queue) >= 24:
                break
        roster = []
        for pid in st.me.players:
            e = ent.get(pid)
            if e:
                roster.append({"name": e.name, "pos": e.position, "team": e.team or "",
                               "pts": round(e.points), "bye": e.bye})

        with self._lock:
            s = self.snap
            s.error = None
            s.picks_made = st.picks_made
            s.on_clock = st.on_the_clock_pick
            s.on_clock_who = who
            s.is_my_turn = st.is_my_turn
            s.synced = st.is_synced
            s.roster = roster
            s.feed = self.ld.pick_feed(12)[::-1]
            s.queue = queue
            s.needs = needs
            s.endgame = endgame
            s.updated = time.time()
            if st.picks_made >= cfg.NUM_TEAMS * cfg.DRAFT_ROUNDS:
                s.status = "draft complete"
            elif st.is_my_turn:
                s.status = "ON THE CLOCK"
            else:
                s.status = "watching"
            if rec and rec.primary:
                e = ent.get(rec.primary.player_id)
                s.pick = {
                    "name": rec.primary.name, "pos": rec.primary.position,
                    "team": (e.team if e else "") or "",
                    "confidence": round(rec.confidence, 3),
                    "margin": rec.margin, "pick_no": rec.pick_no, "round": rec.round_,
                    "adp": round(e.adp, 1) if e else None,
                    "bye": e.bye if e else None,
                }
                s.alternates = [{"name": a.name, "pos": a.position,
                                 "win": round(a.win_rate, 3)} for a in rec.alternatives[:5]]
                s.reasoning = list(rec.reasoning)
                s.survival = {k: round(v, 3) for k, v in rec.survival.items()}
            elif not st.is_my_turn:
                s.pick = None

    def stop(self) -> None:
        self._stop.set()

    def read(self) -> dict:
        with self._lock:
            return asdict(self.snap)


engine = Engine()
app = FastAPI(title="Mishpacha Draft Room")


@app.on_event("startup")
def _start() -> None:
    threading.Thread(target=engine.run, daemon=True).start()


@app.get("/api/state")
def state() -> JSONResponse:
    return JSONResponse(engine.read())


@app.get("/health")
def health() -> JSONResponse:
    s = engine.read()
    return JSONResponse({"ready": s["ready"], "status": s["status"], "error": s["error"]})


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


PAGE = r"""<!doctype html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Draft Room</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans+Condensed:wght@600;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
:root{--bg:#12141A;--panel:#191D26;--panel2:#212633;--ink:#E7EAF1;--muted:#98A0B4;
--faint:#6E7688;--rule:#2A3040;--signal:#E09B3D;--signal-bg:#2B2113;--good:#5BBE8A;--bad:#E08585;--data:#8FAAE4}
@media(prefers-color-scheme:light){:root{--bg:#F3F4F7;--panel:#fff;--panel2:#EAECF1;--ink:#161A22;
--muted:#5C6478;--faint:#8A91A3;--rule:#D8DCE5;--signal:#B26A05;--signal-bg:#FBEFD9;--good:#1F7A4D;--bad:#B03A3A;--data:#3B5DA8}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font-family:"IBM Plex Sans",-apple-system,system-ui,sans-serif;font-size:16px;line-height:1.5}
.wrap{max-width:760px;margin:0 auto;padding:18px 16px 60px;display:flex;flex-direction:column;gap:16px}
h1{font-family:"IBM Plex Sans Condensed",sans-serif;font-size:1.35rem;margin:0;letter-spacing:-.01em}
.mono{font-family:"IBM Plex Mono",monospace;font-variant-numeric:tabular-nums}
header{display:flex;justify-content:space-between;align-items:baseline;gap:12px;
border-bottom:1px solid var(--rule);padding-bottom:12px}
.status{font-family:"IBM Plex Mono",monospace;font-size:.72rem;letter-spacing:.12em;
text-transform:uppercase;color:var(--faint)}
.status.live{color:var(--signal);font-weight:600}
.card{background:var(--panel);border:1px solid var(--rule);border-radius:6px;padding:16px}
.pick{background:var(--signal-bg);border-color:var(--signal);border-left-width:4px}
.pick .k{font-family:"IBM Plex Mono",monospace;font-size:.7rem;letter-spacing:.14em;
text-transform:uppercase;color:var(--signal);font-weight:600}
.pick .name{font-family:"IBM Plex Sans Condensed",sans-serif;font-size:2rem;font-weight:700;margin:4px 0}
.pick .meta{color:var(--muted);font-size:.9rem}
.stats{display:flex;gap:18px;margin-top:10px;flex-wrap:wrap}
.stat{display:flex;flex-direction:column}
.stat .v{font-family:"IBM Plex Mono",monospace;font-size:1.1rem;font-weight:600}
.stat .l{font-size:.7rem;letter-spacing:.1em;text-transform:uppercase;color:var(--faint)}
ul{margin:10px 0 0;padding-left:18px;color:var(--muted);font-size:.88rem}
li{margin:3px 0}
.row{display:flex;justify-content:space-between;gap:10px;padding:6px 0;border-bottom:1px solid var(--rule);font-size:.9rem}
.row:last-child{border:none}
.pos{font-family:"IBM Plex Mono",monospace;font-size:.7rem;color:var(--muted);font-weight:600}
h2{font-family:"IBM Plex Sans Condensed",sans-serif;font-size:.95rem;margin:0 0 8px;
letter-spacing:.06em;text-transform:uppercase;color:var(--faint)}
.feed{font-family:"IBM Plex Mono",monospace;font-size:.78rem;color:var(--muted);
max-height:230px;overflow:auto}
.feed div{padding:2px 0}
.warn{background:var(--panel2);border-left:3px solid var(--bad);padding:10px 12px;
border-radius:4px;font-size:.85rem;color:var(--muted)}
.q{display:grid;grid-template-columns:26px 1fr auto;gap:8px;font-size:.9rem;padding:6px 0;
border-bottom:1px solid var(--rule);align-items:baseline}
.q:last-child{border:none}.q .n{color:var(--faint);font-family:"IBM Plex Mono",monospace}
.q.top{background:var(--signal-bg);margin:0 -8px;padding:8px;border-radius:4px}
.q.top .n{color:var(--signal);font-weight:600}
.q.fills .pos{color:var(--good)}
.qmeta{font-family:"IBM Plex Mono",monospace;font-size:.78rem;color:var(--faint);white-space:nowrap}
.need{display:inline-block;font-family:"IBM Plex Mono",monospace;font-size:.68rem;
letter-spacing:.08em;padding:2px 6px;border-radius:3px;background:var(--panel2);
color:var(--muted);margin-right:5px}
.need.open{background:var(--signal-bg);color:var(--signal);font-weight:600}
details summary{cursor:pointer;color:var(--data);font-size:.88rem}
</style></head><body><div class="wrap">
<header><h1>Draft Room</h1><span class="status" id="st">connecting…</span></header>
<div id="body"></div>
<div class="card"><h2>Queue &mdash; take the highest name left</h2>
<div id="needs" style="margin-bottom:10px"></div>
<div id="queue"></div></div>
<div class="card"><h2>Recent picks</h2><div class="feed" id="feed"></div></div>
</div><script>
const $=s=>document.querySelector(s);
function esc(t){return String(t??"").replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
async function tick(){
 let d; try{ d=await (await fetch('/api/state',{cache:'no-store'})).json() }catch(e){ $('#st').textContent='offline'; return }
 const st=$('#st'); st.textContent=d.status; st.className='status'+(d.is_my_turn?' live':'');
 let h='';
 if(d.error) h+=`<div class="warn"><b>error:</b> ${esc(d.error)}</div>`;
 if(!d.synced) h+=`<div class="warn">Sleeper sent an incomplete pick feed — holding off until it catches up.</div>`;
 if(d.pick && d.is_my_turn){
  const p=d.pick;
  h+=`<div class="card pick"><div class="k">your pick — #${p.pick_no}, round ${p.round}</div>
  <div class="name">${esc(p.name)}</div>
  <div class="meta">${esc(p.pos)} · ${esc(p.team)} · ADP ${p.adp??'—'} · bye ${p.bye??'—'}</div>
  <div class="stats">
   <div class="stat"><span class="v">${Math.round(p.confidence*100)}%</span><span class="l">confidence</span></div>
   <div class="stat"><span class="v">+${p.margin}</span><span class="l">margin</span></div>
  </div>
  <ul>${d.reasoning.map(r=>`<li>${esc(r)}</li>`).join('')}</ul>
  ${d.alternates.length?`<ul>${d.alternates.map(a=>`<li>${esc(a.name)} (${esc(a.pos)}) — ${Math.round(a.win*100)}%</li>`).join('')}</ul>`:''}
  </div>`;
 } else {
  h+=`<div class="card"><h2>On the clock</h2>
  <div style="font-size:1.1rem">pick <span class="mono">#${d.on_clock}</span> — ${esc(d.on_clock_who)}</div>
  <div class="meta" style="color:var(--muted);font-size:.88rem;margin-top:4px">
  ${d.picks_made} picks made · you are slot ${d.my_slot}</div></div>`;
 }
 if(d.roster.length){
  h+=`<div class="card"><h2>Your roster (${d.roster.length})</h2>`+
   d.roster.map(r=>`<div class="row"><span><span class="pos">${esc(r.pos)}</span> ${esc(r.name)}</span>
   <span class="mono" style="color:var(--muted)">${r.pts} · bye ${r.bye??'—'}</span></div>`).join('')+`</div>`;
 }
 $('#body').innerHTML=h;
 $('#feed').innerHTML=d.feed.map(f=>`<div>${esc(f)}</div>`).join('')||'<div>no picks yet</div>';
 // queue is rebuilt server-side every poll, so always re-render it
 const needs=Object.entries(d.needs||{});
 $('#needs').innerHTML = needs.length
   ? 'still need ' + needs.map(([p,n])=>`<span class="need open">${esc(p)}${n>1?' x'+n:''}</span>`).join('')
     + (d.endgame?' <span class="need">endgame — K/DEF now</span>':'')
   : '<span class="need">all starting slots filled</span>';
 $('#queue').innerHTML = d.queue.length
  ? d.queue.map(q=>`<div class="q${q.n===1?' top':''}${q.need?' fills':''}">
     <span class="n">${q.n}</span>
     <span>${esc(q.name)} <span class="pos">${esc(q.pos)}</span>${q.need?' <span class="need open">fills a slot</span>':''}</span>
     <span class="qmeta">ADP ${q.adp} · bye ${q.bye??'—'}</span></div>`).join('')
  : '<div style="color:var(--muted)">building…</div>';
}
tick(); setInterval(tick,2000);
</script></body></html>"""
