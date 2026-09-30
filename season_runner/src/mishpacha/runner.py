"""The one program: a background engine plus a live terminal window.

    mish run            # or double-click Mishpacha.command
    mish install        # once: make the engine a login service (recommended)

The ENGINE (`mish run --daemon`) owns the schedule. After `mish install` it is
a launchd user agent (label com.mishpacha.daemon): it starts at login and is
restarted by launchd if it ever dies, so closing the window no longer stops
anything -- that is exactly how the week-2 waiver run was missed.

The WINDOW (`mish run` with no flag) is a viewer when the engine is up: it
shows this week's matchup and lineup live, every job with its last result and
a countdown, and the engine's activity log. Keys still work; they are handed
to the engine through a request file. Without the engine installed the window
runs the schedule itself, as before, and says so in red.

Missed runs are caught up: open the app Sunday at 10:00 and the 08:30 lineup
job runs immediately, as long as kickoff has not passed. Each scheduled
occurrence runs at most once, tracked in data/runner_state.json, so restarting
the app never repeats a waiver move.

Jobs run as subprocesses (the same `mish` commands you can run by hand), which
keeps the browser automation out of this process and makes the log exactly
what you would have seen in a terminal.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import config as cfg

ROOT = Path(__file__).resolve().parents[2]
# The Home Assistant add-on installs `mish` system-wide and says where.
MISH = os.environ.get("MISHPACHA_MISH") or str(ROOT / ".venv" / "bin" / "mish")
STATE_FILE = ROOT / "data" / "runner_state.json"
LOCK_FILE = ROOT / "data" / "runner.lock"
STATUS_FILE = ROOT / "data" / "runner_status.json"     # engine -> window, every 2s
REQUEST_FILE = ROOT / "data" / "runner_request"         # window -> engine, one key
LOG_FILE = ROOT / "logs" / "runner.log"
LABEL = "com.mishpacha.daemon"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
STALE_AFTER_S = 180          # window shows the engine as unresponsive after this
ANSI = re.compile(r"\x1b\[[0-9;]*m")

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)


@dataclass(frozen=True)
class Job:
    key: str
    label: str
    weekday: int | None        # None = every day
    hour: int
    minute: int
    window_min: int            # how late a missed run is still worth doing
    commands: tuple[tuple[str, ...], ...]
    notify_title: str
    retry: bool = True         # safe to run again after a failure (converges)

    def occurrence_on(self, day: datetime) -> datetime:
        return day.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)

    def last_occurrence(self, now: datetime) -> datetime:
        for back in range(0, 8):
            d = now - timedelta(days=back)
            if self.weekday is not None and d.weekday() != self.weekday:
                continue
            occ = self.occurrence_on(d)
            if occ <= now:
                return occ
        return now - timedelta(days=8)

    def next_occurrence(self, now: datetime) -> datetime:
        for fwd in range(0, 9):
            d = now + timedelta(days=fwd)
            if self.weekday is not None and d.weekday() != self.weekday:
                continue
            occ = self.occurrence_on(d)
            if occ > now:
                return occ
        return now + timedelta(days=8)

    def slot_id(self, occ: datetime) -> str:
        return f"{self.key}@{occ:%Y-%m-%d %H:%M}"

    def due(self, now: datetime, done: set[str]) -> datetime | None:
        """The occurrence to run now, if one is due and not yet done."""
        occ = self.last_occurrence(now)
        if now - occ <= timedelta(minutes=self.window_min) and self.slot_id(occ) not in done:
            return occ
        return None


JOBS: tuple[Job, ...] = (
    Job("refresh", "Daily refresh", None, 7, 0, 16 * 60,
        ((MISH, "sync", "--refresh"), (MISH, "preflight")), "Daily refresh"),
    # Thursday: before TNF locks whoever plays in it (kickoff ~20:15).
    Job("lineup-thu", "Set lineup (Thu)", THU, 18, 0, 130,
        ((MISH, "execute-lineup", "--live"),), "Lineup"),
    # Sunday: before the 9:30 London game; still worth doing until 1pm kickoffs.
    Job("lineup-sun", "Set lineup (Sun)", SUN, 8, 30, 265,
        ((MISH, "execute-lineup", "--live"),), "Lineup"),
    # Tuesday night, before Wednesday waiver processing.
    # Not retried: a run that died after submitting could submit a second claim.
    Job("waivers", "Waiver move (Tue)", TUE, 20, 0, 6 * 60,
        ((MISH, "execute-waivers", "--live"),), "Waivers", retry=False),
    # Wednesday, after claims process: whoever nobody claimed is now a free
    # agent -- an instant add for $0. The rest of the league does this by hand.
    Job("waivers-wed", "Free-agent move (Wed)", WED, 12, 0, 8 * 60,
        ((MISH, "execute-waivers", "--live"),), "Free agents", retry=False),
)

MAX_ATTEMPTS = 3               # per scheduled occurrence, for retryable jobs
RETRY_GAP_S = 5 * 60


def online(host: str = "api.sleeper.app") -> bool:
    import socket
    try:
        socket.getaddrinfo(host, 443)
        return True
    except OSError:
        return False


def starter_slots() -> list[str]:
    """Every starting slot, in Sleeper's order -- ten of them here.

    Derived from the roster positions themselves, not from STARTER_SLOTS: that
    dict has one KEY per position, so counting keys gave 6 + 2 FLEX = 8 and
    silently dropped the kicker and defense from both sides of the projection
    (it showed 41% to win when the true figure was 53%).
    """
    return [p for p in cfg.ROSTER_POSITIONS if p not in ("BN", "IR", "TAXI")]


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"done": [], "last": {}}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["done"] = state.get("done", [])[-200:]
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE_FILE)


def notify(title: str, msg: str) -> None:
    """A Mac notification -- or, inside the Home Assistant add-on, a Home
    Assistant one (persistent card, plus the phone app if one is set up)."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if token:
        import httpx
        body = {"title": f"Season Runner — {title}", "message": msg[:400]}
        for service in ("persistent_notification/create", "notify/notify"):
            try:
                httpx.post(f"http://supervisor/core/api/services/{service}", json=body,
                           headers={"Authorization": f"Bearer {token}"}, timeout=8)
            except Exception:
                pass
        return
    if sys.platform != "darwin":
        return
    subprocess.run(["osascript", "-e",
                    f'display notification {msg[:180]!r} with title "Season Runner" '
                    f'subtitle {title!r}'], capture_output=True)


def keep_awake():
    """Stop the Mac idling to sleep while the engine runs. A server has no
    such problem (and no `caffeinate`)."""
    if sys.platform != "darwin":
        return None
    return subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])


def signed_in() -> bool:
    return (Path.home() / ".mishpacha-browser" / ".logged_in").exists()


# --------------------------------------------------------------------------
# the engine
# --------------------------------------------------------------------------
class Runner:
    def __init__(self, mode: str = "app"):
        self.mode = mode                 # "daemon" (login service) or "app" (window runs it)
        self.state = load_state()
        self.log: deque[tuple[str, str, str]] = deque(maxlen=400)   # (time, style, text)
        self.running: str | None = None
        # RLock: run_job logs (say -> lock) while already holding the lock.
        self.lock = threading.RLock()
        self.started = datetime.now()
        self.matchup: dict = {}
        self.health: dict = {}
        self.stop = threading.Event()
        self.retry_after: dict[str, float] = {}     # slot id -> earliest next attempt
        self.offline_noted = False
        self.signin_noted = False
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    # -- logging ---------------------------------------------------------
    def say(self, text: str, style: str = "") -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.log.append((stamp, style, text))
        with LOG_FILE.open("a") as f:
            f.write(f"{datetime.now():%Y-%m-%d} {stamp} {text}\n")

    # -- jobs ------------------------------------------------------------
    def run_job(self, job: Job, occ: datetime | None, manual: str | None = None) -> None:
        with self.lock:
            if self.running:
                self.say(f"busy with {self.running}; skipped {job.label}", "yellow")
                return
            self.running = job.label
        title = manual or job.label
        self.say(f"▶ {title}", "bold cyan")
        ok, summary = True, ""
        try:
            for cmd in job.commands:
                if manual == "preview waivers":
                    cmd = tuple(c for c in cmd if c != "--live")
                proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True,
                                        env={**os.environ, "COLUMNS": "100", "NO_COLOR": "1"})
                assert proc.stdout
                for raw in proc.stdout:
                    line = ANSI.sub("", raw.rstrip())
                    if not line.strip() or set(line.strip()) <= set("─━═│┃╭╮╰╯┏┓┗┛┡┩┳┻╇ "):
                        continue
                    self.say("   " + line[:140])
                    if re.search(r"already matches|swapped:|standing pat|^add |-> (added|pending|failed|rehearsed|unverified)"
                                 r"|failure\(s\)|warning\(s\)|all systems go|did not take", line):
                        summary = line.strip()[:160]
                rc = proc.wait(timeout=900)
                if rc != 0:
                    ok = False
        except Exception as exc:
            ok = False
            summary = f"{type(exc).__name__}: {exc}"
            self.say("   " + summary, "red")

        result = "ok" if ok else "FAILED"
        with self.lock:
            self.running = None
            self.state.setdefault("last", {})[job.key] = {
                "at": datetime.now().isoformat(timespec="seconds"),
                "result": result + (" (manual)" if manual else ""),
                "summary": summary,
            }
            if occ is not None:
                sid = job.slot_id(occ)
                tries = self.state.setdefault("attempts", {})
                tries[sid] = tries.get(sid, 0) + 1
                # A failed run of a retryable job is left pending so the
                # scheduler tries again inside the window (Sunday's lineup
                # failed once on a network blip and was never retried).
                if ok or not job.retry or tries[sid] >= MAX_ATTEMPTS:
                    self.state.setdefault("done", []).append(sid)
                    tries.pop(sid, None)
                else:
                    self.retry_after[sid] = time.time() + RETRY_GAP_S
                    summary = f"{summary} — will retry" if summary else "will retry"
                    self.state["last"][job.key]["summary"] = summary
            save_state(self.state)
        self.say(f"{'✓' if ok else '✗'} {title}: {summary or result}", "green" if ok else "bold red")
        if not manual or not ok:
            notify(f"{job.notify_title} {'done' if ok else 'FAILED'}", summary or result)

    def scheduler(self) -> None:
        while not self.stop.is_set():
            now = datetime.now()
            done = set(self.state.get("done", []))
            for job in JOBS:
                occ = job.due(now, done)
                if occ and time.time() < self.retry_after.get(job.slot_id(occ), 0):
                    continue                      # failed recently; give it a few minutes
                if occ and any("--live" in c for c in job.commands) and not signed_in():
                    # Nothing can be moved without a Sleeper session. Stay due
                    # (no failure, no notification) until someone signs in.
                    if not self.signin_noted:
                        self.say(f"{job.label} is due but nobody is signed in to Sleeper — waiting", "bold red")
                        self.signin_noted = True
                    continue
                if occ and not self.running:
                    # Just woke from sleep? Wait for the network rather than
                    # burn the run on a DNS error. The job stays due.
                    if not online():
                        if not self.offline_noted:
                            self.say(f"{job.label} is due but the network is not up yet — waiting", "yellow")
                            self.offline_noted = True
                        break
                    self.offline_noted = False
                    late = int((now - occ).total_seconds() // 60)
                    if late > 2:
                        self.say(f"catching up {job.label} (scheduled {occ:%a %H:%M}, {late} min ago)", "yellow")
                    self.run_job(job, occ)
                    break
            self.stop.wait(20)

    # -- live data -------------------------------------------------------
    def refresher(self) -> None:
        from .data import projections as pj
        from .data import sleeper
        from .data.http import get_json
        from .season.matchup import game_states, project_side, win_probability

        players = {}
        weekly: dict = {}
        weekly_for = None
        while not self.stop.is_set():
            try:
                if not players:
                    players = sleeper.fantasy_players()
                week = sleeper.current_week()
                if weekly_for != week or not weekly:
                    weekly = pj.weekly_projections(week=week)
                    weekly_for = week
                games = game_states(week, int(cfg.SEASON))
                mus = get_json(f"{sleeper.BASE}/league/{cfg.LEAGUE_ID}/matchups/{week}",
                               params={"_": int(time.time())}, ttl=0) or []
                rosters = sleeper.rosters(cfg.LEAGUE_ID)
                users = {u["user_id"]: u for u in sleeper.league_users(cfg.LEAGUE_ID)}
                mine = next((r for r in rosters if r.get("owner_id") == cfg.MY_USER_ID), None)
                me = next((m for m in mus if mine and m.get("roster_id") == mine["roster_id"]), None)
                opp = None
                if me:
                    opp = next((m for m in mus if m.get("matchup_id") == me.get("matchup_id")
                                and m.get("roster_id") != me.get("roster_id")), None)

                def team_name(rid):
                    r = next((x for x in rosters if x["roster_id"] == rid), {})
                    u = users.get(r.get("owner_id"), {})
                    return (u.get("metadata") or {}).get("team_name") or u.get("display_name") or f"roster {rid}"

                slots = starter_slots()
                mine_p = theirs_p = None
                if me:
                    mine_p = project_side(slots, me.get("starters") or [],
                                          me.get("starters_points") or [], players, weekly, games)
                if opp:
                    theirs_p = project_side(slots, opp.get("starters") or [],
                                            opp.get("starters_points") or [], players, weekly, games)
                with self.lock:
                    # plain dicts: the snapshot crosses a process boundary as JSON
                    self.matchup = {
                        "week": week,
                        # Tue/Wed these differ: the scoreboard still shows the
                        # finished week while every job is aimed at the next.
                        "active_week": sleeper.active_week(),
                        "me_pts": (me or {}).get("points") or 0.0,
                        "opp_pts": (opp or {}).get("points") or 0.0,
                        "opp_name": team_name(opp["roster_id"]) if opp else "—",
                        "mine": side_dict(mine_p),
                        "theirs": side_dict(theirs_p),
                        "win": win_probability(mine_p, theirs_p) if mine_p and theirs_p else None,
                        "updated": datetime.now().strftime("%H:%M"),
                    }
            except Exception as exc:
                with self.lock:
                    self.matchup["error"] = f"{type(exc).__name__}: {exc}"[:80]
            self.stop.wait(60)

    def check_health(self) -> None:
        try:
            import playwright  # noqa: F401
            pw = True
        except ImportError:
            pw = False
        self.health = {"executor": pw, "signed_in": signed_in()}

    # -- input -----------------------------------------------------------
    def handle_key(self, k: str) -> None:
        by = {j.key: j for j in JOBS}
        if not k:
            return
        if self.running:
            self.say(f"busy with {self.running} — try again when it finishes", "yellow")
            return
        if k == "l":
            threading.Thread(target=self.run_job, args=(by["lineup-sun"], None, "set lineup now"),
                             daemon=True).start()
        elif k == "w":
            threading.Thread(target=self.run_job, args=(by["waivers"], None, "preview waivers"),
                             daemon=True).start()
        elif k == "r":
            threading.Thread(target=self.run_job, args=(by["refresh"], None, "refresh now"),
                             daemon=True).start()
        else:
            self.say(f"unknown key {k!r} — l, w, r or q, then Enter", "yellow")

    def keyboard(self) -> None:
        for raw in sys.stdin:
            k = raw.strip().lower()
            if k == "q":
                self.stop.set()
                return
            self.handle_key(k)

    def requests(self) -> None:
        """Keys typed in a viewer window arrive here, one per file."""
        while not self.stop.is_set():
            try:
                if REQUEST_FILE.exists():
                    k = REQUEST_FILE.read_text().strip().lower()
                    REQUEST_FILE.unlink()
                    self.say(f"window asked: {k!r}", "dim")
                    self.handle_key(k)
            except OSError:
                pass
            self.stop.wait(1)

    # -- snapshot: everything the window needs, as JSON -------------------
    def snapshot(self, write: bool = False) -> dict:
        if self.health:
            self.health["signed_in"] = signed_in()
            if self.health["signed_in"]:
                self.signin_noted = False
        with self.lock:
            snap = {
                "mode": self.mode,
                "pid": os.getpid(),
                "written": datetime.now().isoformat(timespec="seconds"),
                "started": self.started.isoformat(timespec="seconds"),
                "running": self.running,
                "matchup": dict(self.matchup),
                "last": dict(self.state.get("last", {})),
                "health": dict(self.health),
                "log": list(self.log)[-80:],
            }
        if write:
            try:
                tmp = STATUS_FILE.with_suffix(".tmp")
                tmp.write_text(json.dumps(snap))
                tmp.replace(STATUS_FILE)
            except OSError:
                pass
        return snap

    # -- rendering -------------------------------------------------------
    def render(self, width: int = 130, height: int = 50):
        return render(self.snapshot(), width, height)


def side_dict(sp) -> dict | None:
    if sp is None:
        return None
    return {"expected": sp.expected, "sd": sp.sd, "yet_to_play": sp.yet_to_play,
            "playing": sp.playing,
            "lines": [{"slot": l.slot, "name": l.name, "actual": l.actual,
                       "expected": l.expected, "state": l.state} for l in sp.lines]}


def render(snap: dict, width: int = 130, height: int = 50):
    """Draw the dashboard from a snapshot -- the engine's own, or one read
    from data/runner_status.json by a viewer window."""
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    now = datetime.now()
    m = snap.get("matchup") or {}
    last = snap.get("last") or {}
    running = snap.get("running")
    log = [tuple(x) for x in snap.get("log") or []]
    engine = snap.get("mode") == "daemon"

    try:
        age = (now - datetime.fromisoformat(snap["written"])).total_seconds()
        up = str(now - datetime.fromisoformat(snap["started"])).split(".")[0]
    except (KeyError, ValueError):
        age, up = 0.0, "?"
    if snap.get("missing"):
        status = "[bold red]engine not running — run `mish install`[/]"
    elif age > STALE_AFTER_S:
        status = f"[bold red]engine unresponsive for {int(age // 60)} min[/]"
    elif running:
        status = f"[bold yellow]running: {running}[/]"
    else:
        status = "[green]idle — waiting for next job[/]"
    where = "background engine" if engine else "this window is the engine"
    wk, aw = m.get("week", "?"), m.get("active_week")
    week_txt = f"week {wk} final · [bold]playing for week {aw}[/]" if aw and aw != wk else f"week {wk}"
    head = Text.from_markup(
        f"[bold]Season Runner[/]  ·  {week_txt}  ·  {status}  ·  {where}, up {up}")

    # matchup + projection
    mt = Table.grid(padding=(0, 1))
    mine_p, theirs_p, win = m.get("mine"), m.get("theirs"), m.get("win")
    if mine_p and theirs_p:
        me_p, op_p = m["me_pts"], m["opp_pts"]
        lead = "green" if me_p >= op_p else "red"
        mt.add_row(Text.from_markup(
            f"[bold {lead}]You {me_p:.2f}[/]  vs  {m['opp_name']} {op_p:.2f}"))

        wcol = "green" if win >= 0.6 else ("red" if win <= 0.4 else "yellow")
        filled = int(round(win * 20))
        bar = f"[{wcol}]" + "█" * filled + "[/][dim]" + "░" * (20 - filled) + "[/]"
        mt.add_row(Text.from_markup(
            f"[bold {wcol}]{win:.0%} to win[/]  {bar}"))
        mt.add_row(Text.from_markup(
            f"projected [bold]{mine_p['expected']:.1f}[/] (±{mine_p['sd']:.0f})  –  "
            f"{theirs_p['expected']:.1f} (±{theirs_p['sd']:.0f})"))
        mt.add_row(Text.from_markup(
            f"[dim]you: {mine_p['yet_to_play']} yet to play, {mine_p['playing']} playing  ·  "
            f"them: {theirs_p['yet_to_play']} yet to play, {theirs_p['playing']} playing[/]"))

        lt = Table(box=None, show_header=True, pad_edge=False, header_style="dim")
        lt.add_column("", style="dim", width=4)
        lt.add_column("starter")
        lt.add_column("pts", justify="right")
        lt.add_column("proj", justify="right")
        lt.add_column("", style="dim")
        for l in mine_p["lines"]:
            mark = {"post": "final", "in": "live", "pre": "", "bye": "no game"}.get(l["state"], "")
            pts = f"{l['actual']:.1f}" if l["state"] in ("post", "in") else "–"
            lt.add_row(l["slot"], l["name"][:20], pts, f"{l['expected']:.1f}",
                       Text(mark, style="green" if l["state"] == "in" else "dim"))
        mt.add_row(lt)
        mt.add_row(Text(f"updated {m.get('updated')}  ·  win odds assume normal spread of outcomes",
                        style="dim"))
    else:
        mt.add_row(Text(m.get("error") or "loading matchup…", style="dim"))
    matchup_panel = Panel(mt, title="This week", border_style="blue")

    # jobs
    jt = Table(box=None, pad_edge=False)
    for c in ("Job", "Next run", "Last result"):
        jt.add_column(c)
    for job in JOBS:
        nxt = job.next_occurrence(now)
        delta = nxt - now
        hrs = int(delta.total_seconds() // 3600)
        eta = f"{nxt:%a %H:%M}  (in {hrs // 24}d {hrs % 24}h)" if hrs >= 24 else \
              f"{nxt:%a %H:%M}  (in {hrs}h {int(delta.total_seconds() % 3600 // 60)}m)"
        l = last.get(job.key)
        if l:
            style = "green" if l["result"].startswith("ok") else "red"
            res = f"[{style}]{l['result']}[/] {datetime.fromisoformat(l['at']):%a %H:%M} — {l['summary'][:48]}"
        else:
            res = "[dim]not run yet[/]"
        jt.add_row(job.label, eta, Text.from_markup(res))
    h = snap.get("health") or {}
    health = Text.from_markup(
        f"executor {'[green]ready[/]' if h.get('executor') else '[red]missing playwright[/]'}  ·  "
        f"Sleeper {'[green]signed in[/]' if h.get('signed_in') else '[red]not signed in — run mish login[/]'}  ·  "
        + ("[green]survives closing this window[/]" if engine
           else "[red]stops when this window closes — run mish install[/]"))
    jobs_panel = Panel(Group(jt, Text(""), health), title="Automation", border_style="cyan")

    quit_hint = "close window (engine keeps running)" if engine else "quit (stops everything)"
    keys = Text.from_markup(
        "[dim]type a key + Enter:[/]  [bold]l[/] set lineup now   [bold]w[/] preview waiver move   "
        f"[bold]r[/] refresh   [bold]q[/] {quit_hint}     [dim]log: logs/runner.log[/]")

    # Narrow window: stack panels instead of squeezing two columns. Side by
    # side below ~120 cols crushed names to "Dra… Maye" and points to "12…".
    if width >= 120:
        top = Table.grid(expand=True, padding=(0, 1))
        top.add_column(ratio=2)
        top.add_column(ratio=3)
        top.add_row(matchup_panel, jobs_panel)
        body = [top]
        used = 27
    else:
        body = [matchup_panel, jobs_panel]
        used = 43
    room = max(4, height - used)
    at = Text()
    for stamp, style, text in log[-room:]:
        at.append(f"{stamp} ", style="dim")
        at.append(text + "\n", style=style or None)
    activity = Panel(at or Text("nothing yet", style="dim"), title="Activity",
                     border_style="magenta")
    return Group(head, *body, activity, keys)


def schedule_table() -> str:
    now = datetime.now()
    state = load_state()
    done = set(state.get("done", []))
    lines = []
    for job in JOBS:
        due = job.due(now, done)
        lines.append(f"{job.label:<20} next {job.next_occurrence(now):%a %b %d %H:%M}"
                     + (f"   DUE NOW (catch-up for {due:%a %H:%M})" if due else ""))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# login service (so the engine survives the window being closed)
# --------------------------------------------------------------------------
def plist_xml() -> str:
    path = os.environ.get("PATH", "")
    for extra in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"):
        if extra not in path.split(":"):
            path += ":" + extra
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array><string>{MISH}</string><string>run</string><string>--daemon</string></array>
    <key>WorkingDirectory</key><string>{ROOT}</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>10</integer>
    <key>ProcessType</key><string>Interactive</string>
    <key>StandardOutPath</key><string>{ROOT / "logs" / "daemon.out"}</string>
    <key>StandardErrorPath</key><string>{ROOT / "logs" / "daemon.err"}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>{path}</string>
        <key>HOME</key><string>{Path.home()}</string>
        <key>LANG</key><string>en_US.UTF-8</string>
    </dict>
</dict>
</plist>
"""


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def engine_loaded() -> bool:
    return _launchctl("print", f"gui/{os.getuid()}/{LABEL}").returncode == 0


def engine_alive() -> bool:
    """True when the login-service engine wrote a snapshot recently."""
    try:
        snap = json.loads(STATUS_FILE.read_text())
        age = (datetime.now() - datetime.fromisoformat(snap["written"])).total_seconds()
        return snap.get("mode") == "daemon" and age < STALE_AFTER_S
    except (OSError, ValueError, KeyError):
        return False


def install() -> str:
    """Write the launchd agent and load it. Idempotent.

    Returns 'installed', 'updated', 'ok' or 'failed: ...'. A changed plist is
    not restarted while a job is running -- it takes effect on the engine's
    next restart instead of interrupting a lineup move half way."""
    (ROOT / "logs").mkdir(parents=True, exist_ok=True)
    xml = plist_xml()
    try:
        changed = not PLIST.exists() or PLIST.read_text() != xml
        if changed:
            PLIST.parent.mkdir(parents=True, exist_ok=True)
            PLIST.write_text(xml)
    except OSError as exc:
        return f"failed: {exc}"
    loaded = engine_loaded()
    if loaded and changed:
        try:
            busy = json.loads(STATUS_FILE.read_text()).get("running")
        except (OSError, ValueError):
            busy = None
        if not busy:
            _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
            loaded = False
    if not loaded:
        res = _launchctl("bootstrap", f"gui/{os.getuid()}", str(PLIST))
        if res.returncode != 0 and "already" not in (res.stderr + res.stdout).lower():
            return f"failed: {(res.stderr or res.stdout).strip()[:160]}"
    _hand_over_to_service()
    return "ok" if loaded and not changed else ("installed" if changed else "updated")


def _service_pid() -> int | None:
    m = re.search(r"\bpid = (\d+)", _launchctl("print", f"gui/{os.getuid()}/{LABEL}").stdout)
    return int(m.group(1)) if m else None


def _hand_over_to_service() -> None:
    """An engine started by hand (`mish run --daemon` in a terminal, or a
    window running the schedule itself) holds the lock, so the launchd copy
    would wait forever. Ask it to stop -- unless it is in the middle of a job."""
    import signal
    try:
        snap = json.loads(STATUS_FILE.read_text())
        age = (datetime.now() - datetime.fromisoformat(snap["written"])).total_seconds()
    except (OSError, ValueError, KeyError):
        return
    pid, svc = snap.get("pid"), _service_pid()
    if age > STALE_AFTER_S or not pid or pid == svc or snap.get("running"):
        return
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def uninstall() -> None:
    _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    try:
        PLIST.unlink()
    except FileNotFoundError:
        pass


# --------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------
def _serve(lock_fh) -> None:
    """The engine as a login service. Runs until launchd stops it; never draws."""
    import signal

    r = Runner(mode="daemon")
    r.check_health()
    caffeinate = keep_awake()
    signal.signal(signal.SIGTERM, lambda *_: r.stop.set())
    r.say("engine started as a login service — runs with no window open; missed runs are caught up", "bold")
    if not r.health["executor"]:
        r.say("playwright missing: run `uv pip install -e .` — moves will fail until then", "bold red")
    if not r.health["signed_in"]:
        r.say("not signed in to Sleeper: run `mish login` in a terminal", "bold red")
    for target in (r.scheduler, r.refresher, r.requests):
        threading.Thread(target=target, daemon=True).start()
    try:
        while not r.stop.is_set():
            r.snapshot(write=True)
            r.stop.wait(2)
    finally:
        r.say("engine stopping", "yellow")
        r.snapshot(write=True)
        if caffeinate:
            caffeinate.terminate()
        fcntl.flock(lock_fh, fcntl.LOCK_UN)


def _view(lock_fh) -> bool:
    """The window: draws the engine's snapshot; keys go to the engine.

    Returns True when the user quit. Returns False -- holding the lock -- if
    the engine went silent and nobody owns the schedule, so the caller can
    run it in this window rather than show a dead dashboard."""
    from rich.console import Console
    from rich.live import Live

    stop = threading.Event()

    def engine_gone() -> bool:
        try:
            snap = json.loads(STATUS_FILE.read_text())
            age = (datetime.now() - datetime.fromisoformat(snap["written"])).total_seconds()
            if age < STALE_AFTER_S:
                return False
        except (OSError, ValueError, KeyError):
            pass
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True                     # free: no engine anywhere. We own it now.
        except BlockingIOError:
            return False                    # someone holds it; keep watching

    def keyboard():
        for raw in sys.stdin:
            k = raw.strip().lower()
            if k == "q":
                stop.set()
                return
            if k:
                try:
                    REQUEST_FILE.write_text(k)
                except OSError:
                    pass

    threading.Thread(target=keyboard, daemon=True).start()
    console = Console()

    def load() -> dict:
        try:
            return json.loads(STATUS_FILE.read_text())
        except (OSError, ValueError):
            return {"mode": "daemon", "missing": True, "log": [], "matchup": {}}

    try:
        with Live(render(load()), console=console, refresh_per_second=2, screen=True) as live:
            while not stop.is_set():
                live.update(render(load(), console.width, console.height))
                if engine_gone():
                    return False
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    print("Window closed. The engine keeps running in the background "
          "(mish run --check shows the schedule; logs/runner.log has everything).")
    return True


def _run_in_window(lock_fh) -> None:
    """Pre-service behaviour: this window owns the schedule."""
    from rich.console import Console
    from rich.live import Live

    r = Runner(mode="app")
    r.check_health()
    # keep the Mac from idling to sleep while the app is open
    caffeinate = keep_awake()
    r.say("running IN THIS WINDOW — closing it stops everything. Run `mish install` once to fix that.", "bold red")
    if not r.health["executor"]:
        r.say("playwright missing: run `uv pip install -e .` — moves will fail until then", "bold red")
    if not r.health["signed_in"]:
        r.say("not signed in to Sleeper: run `mish login` in another terminal", "bold red")

    # `mish install` hands the schedule to the login service by asking this
    # window to stop; a clean stop releases the lock and says what happened.
    import signal
    signal.signal(signal.SIGTERM, lambda *_: r.stop.set())

    for target in (r.scheduler, r.refresher, r.keyboard, r.requests):
        threading.Thread(target=target, daemon=True).start()

    console = Console()
    try:
        with Live(r.render(), console=console, refresh_per_second=2, screen=True) as live:
            while not r.stop.is_set():
                live.update(render(r.snapshot(write=True), console.width, console.height))
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        r.stop.set()
        if caffeinate:
            caffeinate.terminate()
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        if engine_loaded():
            print("Handed over to the login service. Reopen Mishpacha.command to watch it.")
        else:
            print("Mishpacha stopped. Nothing runs until you open it again.")


def main(check: bool = False, daemon: bool = False) -> None:
    if check:
        print(schedule_table())
        return

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = open(LOCK_FILE, "w")

    if daemon:
        # Wait for any window that is running the schedule itself to quit,
        # then take over. launchd restarts us if we ever exit.
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        _serve(lock_fh)
        return

    if engine_loaded() and not engine_alive():
        # Service exists but has not written a snapshot yet (just booted, or
        # waiting for a window to release the lock). Give it a moment.
        for _ in range(15):
            if engine_alive():
                break
            time.sleep(1)

    if not engine_alive():
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _run_in_window(lock_fh)
            return
        except BlockingIOError:
            pass              # another window owns the schedule; watch it
    # Watch the engine. If it dies and nothing else takes the schedule, this
    # window takes it -- a viewer must never leave the season unattended.
    if not _view(lock_fh):
        _run_in_window(lock_fh)
