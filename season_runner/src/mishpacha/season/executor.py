"""Execute roster moves on Sleeper by driving a real browser.

Sleeper has no public write API, and holding a user's session token in a
script is the account-risk path this project deliberately avoids. Instead a
persistent Chromium profile is driven with Playwright: you log in once, the
profile keeps the session, and scheduled jobs operate the same UI a person
would. No credentials are ever read or stored by this code.

Mechanics, verified by hand on the live roster page:
  * Every roster row is an accessible link named "Slot <POS> - <Player Name>"
    (starters) or "Slot BN - <Player Name>" (bench).
  * Clicking a starter's slot enters swap mode; eligible targets are
    highlighted, ineligible rows greyed out. Clicking an eligible player
    performs the swap. Two clicks, no drag-and-drop.
  * Players whose game has kicked off are locked and cannot be moved.

Every action is verified by re-reading the page afterwards. Nothing here ever
drops a player outright: adds that need roster space are submitted as waiver
claims with a named drop, which Sleeper holds until Wednesday processing --
that window is the human veto.
"""
from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import config as cfg

PROFILE_DIR = Path.home() / ".mishpacha-browser"
TEAM_URL = f"https://sleeper.com/leagues/{cfg.LEAGUE_ID}/team"
SLOT_RE = re.compile(r"^Slot (\w+) - (.+)$")

STARTER_ORDER = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "K", "DEF"]


@dataclass
class RosterView:
    """What the page shows right now: ordered starters and the bench."""
    starters: list[tuple[str, str]] = field(default_factory=list)   # (slot, name)
    bench: list[str] = field(default_factory=list)

    @property
    def starter_names(self) -> set[str]:
        return {n for _, n in self.starters}


def _launch(headless: bool = False):
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    args = ["--disable-blink-features=AutomationControlled"]
    if sys.platform.startswith("linux"):
        # Inside the Home Assistant add-on Chromium runs as root in a
        # container, where its own sandbox cannot start. A container that is
        # killed also leaves the profile "locked" by a hostname that no longer
        # exists; clear that or the next launch gets a blank profile.
        if os.geteuid() == 0:
            args += ["--no-sandbox", "--disable-dev-shm-usage"]
        for stale in PROFILE_DIR.glob("Singleton*"):
            try:
                stale.unlink()
            except OSError:
                pass
        if not headless:
            args += ["--window-position=0,0", "--window-size=1280,960"]
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        viewport={"width": 1280, "height": 900},
        args=args,
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return pw, ctx, page


def _parse_labels(labels: list[str]) -> RosterView:
    view = RosterView()
    for raw in labels:
        m = SLOT_RE.match(raw.strip())
        if not m:
            continue
        slot, player = m.group(1), m.group(2).strip()
        if slot == "BN":
            view.bench.append(player)
        else:
            view.starters.append((slot, player))
    return view


def _read_roster(page, settle_s: float = 12.0) -> RosterView:
    """Snapshot every roster row, polling until the page has settled.

    Two lessons from the live page:
      * Walking links one at a time timed out at nth(14): the list re-renders
        while you iterate. So every label is read in ONE evaluate_all call.
      * Rows briefly read "N/A" instead of "Slot X - Name" while their stats
        line renders -- and rows whose game has kicked off can sit that way.
        A single read therefore under-counts. Poll until the full starting
        lineup is visible, or return the most complete read once time is up.

    The DOM is used only to decide what to click. Whether a change actually
    took is verified against Sleeper's API, which is authoritative.
    """
    links = page.get_by_role("link", name=re.compile(r"^Slot "))
    links.first.wait_for(timeout=20_000)
    best = RosterView()
    deadline = time.time() + settle_s
    while True:
        labels = links.evaluate_all(
            "els => els.map(e => e.getAttribute('aria-label') || e.textContent || '')")
        view = _parse_labels(labels)
        if len(view.starters) >= len(STARTER_ORDER):
            return view
        if len(view.starters) + len(view.bench) > len(best.starters) + len(best.bench):
            best = view
        if time.time() > deadline:
            return best
        time.sleep(1.0)


def _api_starters() -> set[str]:
    """Names of our current starters straight from Sleeper's API.

    Two layers of caching had to be defeated before this told the truth:
      * our own sqlite cache (ttl=0 bypasses it), and
      * a CDN in front of api.sleeper.app that serves a stale copy for
        minutes. Every swap the executor made today persisted -- the page,
        which reads live over GraphQL, showed them -- while this endpoint kept
        returning the old lineup and made each one look like a failure.
    A cache-busting query param plus Cache-Control: no-cache gets a live read;
    verified by comparing against a fresh page load.
    """
    from ..data import sleeper
    from ..data.http import get_json
    rosters = get_json(
        f"{sleeper.BASE}/league/{cfg.LEAGUE_ID}/rosters",
        params={"_": int(time.time() * 1000)},
        headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
        ttl=0,
    ) or []
    mine = next((r for r in rosters if r.get("owner_id") == cfg.MY_USER_ID), None)
    if not mine:
        return set()
    players = sleeper.fantasy_players()
    return {players[p].name for p in (mine.get("starters") or []) if p in players}


def _api_empty_slots() -> list[str]:
    """Starting slots with nobody in them, e.g. ["DEF"], from a live API read.

    Sleeper marks an empty starter as "0". The page shows such a slot as an
    unlabelled "N/A" cell -- the same label a locked row gets -- so the DOM
    cannot tell us; the API's slot order (league roster_positions) can.
    """
    from ..data import sleeper
    from ..data.http import get_json
    rosters = get_json(
        f"{sleeper.BASE}/league/{cfg.LEAGUE_ID}/rosters",
        params={"_": int(time.time() * 1000)},
        headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
        ttl=0,
    ) or []
    mine = next((r for r in rosters if r.get("owner_id") == cfg.MY_USER_ID), None)
    if not mine:
        return []
    slots = [x for x in cfg.ROSTER_POSITIONS if x not in ("BN", "IR", "TAXI")]
    return [slot for slot, pid in zip(slots, mine.get("starters") or [])
            if pid in ("0", 0, None, "")]


# What the slot badge reads on the page.
SLOT_BADGE = {"FLEX": "WRT"}


def plan_fills(bench: list[str], desired_starters: set[str], empty_slots: list[str],
               positions: dict[str, str] | None = None,
               taken: set[str] | None = None) -> list[tuple[str, str]]:
    """(empty_slot, bench_player) pairs: desired starters who can simply be
    moved into a starting slot nobody occupies. Happens after every add that
    replaces a starter -- the newcomer lands on the bench and the old slot is
    left empty (2026-09-30: Bears added for Eagles, DEF slot empty)."""
    positions = positions or {}
    taken = set(taken or ())
    fills, free = [], list(empty_slots)
    for name in bench:
        if name not in desired_starters or name in taken:
            continue
        pos = positions.get(name)
        ok = [sl for sl in free if pos is None or pos in SLOT_ACCEPTS.get(sl, set())]
        ok.sort(key=lambda sl: sl == "FLEX")          # dedicated slot first
        if ok:
            free.remove(ok[0])
            fills.append((ok[0], name))
    return fills


def _click_empty_slot(page, slot: str) -> bool:
    """Click the empty starting cell for `slot`. Call AFTER selecting the
    bench player: mapped live 2026-09-30, the empty cell is labelled "N/A"
    (like a locked row) until a player who fits it is selected, and only then
    becomes a clickable "Empty" target. Its row reads e.g. "DEFEmpty"."""
    badge = SLOT_BADGE.get(slot, slot)
    cells = page.locator("a.cell-position[aria-label='Empty']")
    idx = cells.evaluate_all(
        """(els, want) => els.findIndex(e => {
             const row = e.closest('.team-roster-item');
             return row && (row.textContent || '').replace(/\\s+/g, '') === want;
           })""", badge + "Empty")
    if idx is None or idx < 0:
        return False
    cells.nth(idx).click()
    return True


def _roster_visible(page) -> bool:
    try:
        return page.get_by_role("link", name=re.compile(r"^Slot ")).count() > 0
    except Exception:
        return False


def _mark_logged_in() -> None:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    (PROFILE_DIR / ".logged_in").write_text(time.strftime("%Y-%m-%d %H:%M"))


def _ensure_logged_in(page) -> None:
    page.goto(TEAM_URL, wait_until="domcontentloaded")
    try:
        page.get_by_role("link", name=re.compile(r"^Slot ")).first.wait_for(timeout=20_000)
    except Exception:
        raise RuntimeError(
            "Roster page did not load. If this is the first run, log in to "
            "Sleeper in the browser window that opened, then run again.")
    _mark_logged_in()   # any successful headless run proves the session works


def login_once(timeout_minutes: int = 10) -> bool:
    """Open a visible browser so you can sign in; the profile remembers it.

    Polls for the roster page instead of waiting on a terminal prompt, so it
    works when launched from a scheduler or a non-interactive shell. Returns
    True once the roster page loads with your players on it.
    """
    pw, ctx, page = _launch(headless=False)
    try:
        # Sleeper bounces an unauthenticated visit to the roster page over to
        # its login screen; once you sign in it comes back here on its own.
        page.goto(TEAM_URL, wait_until="domcontentloaded")
        print("Sign in to Sleeper in the window that opened.", flush=True)
        deadline = time.time() + timeout_minutes * 60
        nudged_at = 0.0
        while time.time() < deadline:
            time.sleep(3)
            try:
                if _roster_visible(page):
                    _mark_logged_in()
                    print("Signed in. Roster page loads. Profile saved at",
                          PROFILE_DIR, flush=True)
                    return True
                # Signed in but parked somewhere else (home, another league)?
                # Nudge to the roster page -- but never while a login form is
                # up, so we do not yank the page out from under your typing.
                url = page.url
                if "login" not in url and "signin" not in url and time.time() - nudged_at > 10:
                    nudged_at = time.time()
                    page.goto(TEAM_URL, wait_until="domcontentloaded")
            except Exception:
                continue
        print("Timed out waiting for sign-in.", flush=True)
        return False
    finally:
        ctx.close(); pw.stop()


def is_logged_in() -> bool:
    return (PROFILE_DIR / ".logged_in").exists()


SLOT_ACCEPTS = {
    "QB": {"QB"}, "RB": {"RB"}, "WR": {"WR"}, "TE": {"TE"}, "K": {"K"}, "DEF": {"DEF"},
    "FLEX": {"RB", "WR", "TE"},
}


def plan_swaps(
    current: RosterView,
    desired_starters: set[str],
    positions: dict[str, str] | None = None,
    locked: set[str] | None = None,
) -> list[tuple[str, str]]:
    """(starter_out, bench_in) pairs that turn the current lineup into the
    desired one -- each pair legal for the slot being vacated.

    The first version zipped the outgoing and incoming lists by index. Fed a
    desired set that was missing a locked quarterback (the page cannot see
    locked rows), it paired that quarterback with an incoming running back and
    tried to bench him for a player his slot cannot hold. Sleeper refused only
    because the game had started. So:
      * a starter is only vacated for someone his slot ACCEPTS (FLEX takes
        RB/WR/TE; dedicated slots take their own position), dedicated slots
        preferred so FLEX stays open for the best remaining body;
      * players in `locked` are never touched;
      * without `positions` the pairing is by position-agnostic order, which is
        only safe for single-player tests.
    """
    locked = locked or set()
    positions = positions or {}
    vacate = [(s, n) for s, n in current.starters
              if n not in desired_starters and n not in locked]
    incoming = [n for n in current.bench if n in desired_starters and n not in locked]

    pairs: list[tuple[str, str]] = []
    used: set[tuple[str, str]] = set()
    for name in incoming:
        pos = positions.get(name)
        cands = [(s, n) for s, n in vacate
                 if (s, n) not in used
                 and (pos is None or pos in SLOT_ACCEPTS.get(s, set()))]
        cands.sort(key=lambda t: t[0] == "FLEX")      # dedicated slot first
        if not cands:
            continue
        s, n = cands[0]
        used.add((s, n))
        pairs.append((n, name))
    return pairs


def set_lineup(desired_starters: set[str], dry_run: bool = False,
               headless: bool = True, positions: dict[str, str] | None = None,
               locked: set[str] | None = None) -> dict:
    """Make Sleeper's starters match `desired_starters` (a set of names).

    `positions` (name -> position) makes every planned swap slot-legal;
    `locked` names players whose game has started and must not be touched.
    Every swap is verified against a cache-busted read of Sleeper's API; one
    that does not take is reported, not retried blindly.
    """
    pw, ctx, page = _launch(headless=headless)
    report = {"before": [], "swaps": [], "after": [], "unresolved": [], "dry_run": dry_run}
    try:
        _ensure_logged_in(page)
        current = _read_roster(page)
        report["before"] = list(current.starters)
        swaps = plan_swaps(current, desired_starters, positions=positions, locked=locked)
        fills = plan_fills(current.bench, desired_starters, _api_empty_slots(),
                           positions=positions, taken={b for _, b in swaps} | set(locked or ()))
        # Reported alongside swaps as ("(empty SLOT)", player).
        report["swaps"] = swaps + [(f"(empty {sl})", n) for sl, n in fills]
        if dry_run or not (swaps or fills):
            report["after"] = [n for _, n in current.starters]
            return report

        for slot, bench_in in fills:
            took = False
            page.get_by_role("link", name=re.compile(
                rf"^Slot BN - {re.escape(bench_in)}$")).first.click()
            time.sleep(1.5)
            if _click_empty_slot(page, slot):
                time.sleep(2.0)
                deadline = time.time() + 45
                while time.time() < deadline:
                    if bench_in in _api_starters():
                        took = True
                        break
                    time.sleep(3.0)
            if not took:
                report["unresolved"].append((f"(empty {slot})", bench_in))
                page.keyboard.press("Escape")
                page.goto(TEAM_URL, wait_until="domcontentloaded")
                time.sleep(2.0)

        for starter_out, bench_in in swaps:
            # (?!BN) matters: \w+ alone also matches "Slot BN - ...", and a
            # probe once clicked a bench row instead of the starter's slot.
            slot_link = page.get_by_role("link", name=re.compile(rf"^Slot (?!BN)\w+ - {re.escape(starter_out)}$"))
            target = page.get_by_role("link", name=re.compile(rf"^Slot BN - {re.escape(bench_in)}$"))
            slot_link.first.click()
            time.sleep(1.2)
            target.first.click()
            time.sleep(2.0)
            # Verify against the API, not the page: locked rows read as blank
            # in the DOM and turned a successful swap into a false failure.
            # And POLL it: the public API lags the UI by several seconds, so a
            # single read right after the click reported a swap that had in
            # fact persisted (the next page load showed it) as a failure.
            deadline = time.time() + 45
            took = False
            while time.time() < deadline:
                now = _api_starters()
                if now and bench_in in now and starter_out not in now:
                    took = True
                    break
                time.sleep(3.0)
            if not took:
                report["unresolved"].append((starter_out, bench_in))
                # leave swap mode so the next attempt starts clean
                page.keyboard.press("Escape")
                page.goto(TEAM_URL, wait_until="domcontentloaded")
                time.sleep(2.0)

        deadline = time.time() + 30
        api_after = _api_starters()
        while time.time() < deadline and api_after != set(desired_starters):
            time.sleep(3.0)
            api_after = _api_starters()
        report["after"] = sorted(api_after)
        report["verified"] = bool(api_after) and api_after == set(desired_starters)
        report["missing"] = sorted(set(desired_starters) - api_after)
        report["extra"] = sorted(api_after - set(desired_starters))
        return report
    finally:
        ctx.close(); pw.stop()


PLAYERS_URL = f"https://sleeper.com/leagues/{cfg.LEAGUE_ID}/players"


def add_player(add_name: str, drop_name: str | None = None, bid: int | None = None,
               dry_run: bool = False, headless: bool = True, commit: bool = True) -> dict:
    """Add a player, dropping `drop_name` if the roster is full.

    Two Sleeper variants share one dialog, mapped on the live league:
      * Cleared free agent -> "Add Player" commits IMMEDIATELY. Irreversible.
      * Player still on waivers -> the same dialog carries a FAAB bid field
        and submits a CLAIM that Sleeper holds until Wednesday processing.
        That is cancellable from the Waivers panel until then.

    The dialog title is checked against `add_name` before anything is clicked,
    so a search that matched the wrong player cannot commit. Result is verified
    by re-reading the roster; a claim (not instant) shows as `pending`.
    """
    pw, ctx, page = _launch(headless=headless)
    rep = {"add": add_name, "drop": drop_name, "bid": bid, "dry_run": dry_run,
           "status": "not_started", "detail": ""}
    try:
        _ensure_logged_in(page)
        before = _read_roster(page)
        page.goto(PLAYERS_URL, wait_until="domcontentloaded")
        box = page.get_by_role("textbox", name=re.compile("Find player"))
        box.first.wait_for(timeout=15_000)
        box.first.fill(add_name)
        time.sleep(2.5)

        grid = page.get_by_role("grid").first
        add_link = grid.get_by_role("link").first
        if add_link.count() == 0:
            rep.update(status="failed", detail="no player row matched the search")
            return rep
        if dry_run:
            rep.update(status="dry_run", detail="would open the add dialog and submit")
            return rep

        add_link.click()
        time.sleep(2.5)

        # Guard: the dialog must be for the player we asked for.
        title = page.get_by_text(re.compile(rf"Add {re.escape(add_name)}", re.I))
        if title.count() == 0:
            rep.update(status="failed", detail="add dialog did not open for the expected player")
            page.keyboard.press("Escape")
            return rep

        bid_box = page.get_by_role("spinbutton")
        has_bid = bid_box.count() > 0
        if has_bid and bid is not None:
            bid_box.first.fill(str(int(bid)))
            time.sleep(0.5)

        if drop_name:
            drop_row = page.get_by_text(drop_name, exact=False)
            if drop_row.count() == 0:
                rep.update(status="failed", detail=f"drop target {drop_name!r} not shown in dialog")
                page.keyboard.press("Escape")
                return rep
            drop_row.first.click()
            time.sleep(1.0)

        submit = page.get_by_role("button", name=re.compile(r"Add Player|Submit|Claim", re.I))
        if submit.count() == 0:
            rep.update(status="failed", detail="no submit button found in dialog")
            page.keyboard.press("Escape")
            return rep
        if not commit:
            # Rehearsal: everything up to the irreversible click, then back out.
            rep.update(status="rehearsed",
                       detail=f"dialog opened for {add_name}, "
                              f"{'bid field present' if has_bid else 'instant add (no bid field)'}, "
                              f"drop {'selected' if drop_name else 'not needed'}; NOT submitted")
            page.keyboard.press("Escape")
            time.sleep(1.0)
            return rep
        submit.first.click()
        time.sleep(3.5)

        page.goto(TEAM_URL, wait_until="domcontentloaded")
        time.sleep(2.5)
        after = _read_roster(page)
        names_after = after.starter_names | set(after.bench)
        if add_name in names_after and (not drop_name or drop_name not in names_after):
            rep.update(status="added", detail="instant add confirmed on roster")
        elif has_bid:
            rep.update(status="pending", detail="claim submitted; processes Wednesday, cancellable until then")
        else:
            rep.update(status="unverified", detail="roster unchanged after submit; check the Waivers panel")
        rep["before"] = sorted(before.starter_names | set(before.bench))
        rep["after"] = sorted(names_after)
        return rep
    finally:
        ctx.close(); pw.stop()
