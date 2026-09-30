"""mish -- command line for the Season Runner draft/season engine."""
from __future__ import annotations

import time

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import config as cfg
from .data import sleeper
from .draft.recommend import DraftBrain
from .draft.state import DraftState
from .live import LiveDraft
from .model.board import build_board

app = typer.Typer(add_completion=False, help="Automated fantasy football engine.")
con = Console()

_cache: dict = {}


def get_board(refresh: bool = False):
    if refresh or "board" not in _cache:
        with con.status("[cyan]building board (projections + ADP + ECR)..."):
            _cache["board"], _cache["diag"] = build_board(ttl=0 if refresh else 3600)
    return _cache["board"], _cache["diag"]


def get_brain(refresh: bool = False) -> DraftBrain:
    board, _ = get_board(refresh)
    if refresh or "brain" not in _cache:
        _cache["brain"] = DraftBrain(board, my_slot=cfg.MY_DRAFT_SLOT)
    return _cache["brain"]


@app.command()
def sync(refresh: bool = typer.Option(False, "--refresh", help="bypass caches")):
    """Verify league state, membership and data freshness."""
    lg = sleeper.league(cfg.LEAGUE_ID)
    dr = sleeper.draft(cfg.DRAFT_ID)
    users = sleeper.league_users(cfg.LEAGUE_ID)
    rosters = sleeper.rosters(cfg.LEAGUE_ID)

    t = Table(title=f"{lg['name']}  ({lg['season']})", show_header=False, box=None)
    t.add_row("league status", lg["status"])
    t.add_row("draft status", dr["status"])
    t.add_row("draft start", str(dr.get("start_time") or "[yellow]not scheduled[/]"))
    t.add_row("draft order set", "[green]yes[/]" if dr.get("draft_order") else "[yellow]no (commish hasn't set it)[/]")
    t.add_row("teams joined", f"{len(users)} / {lg['total_rosters']}")
    con.print(t)

    me = next((u for u in users if u["user_id"] == cfg.MY_USER_ID), None)
    if me:
        r = next((x for x in rosters if x.get("owner_id") == cfg.MY_USER_ID), None)
        con.print(f"[green]you are in the league[/] as {me['display_name']} (roster_id {r['roster_id'] if r else '?'})")
    else:
        con.print(Panel(
            f"[yellow]You have NOT joined the league yet.[/]\n"
            f"Your Sleeper account '{cfg.MY_USERNAME}' exists (user_id {cfg.MY_USER_ID}) "
            f"but is not a member.\n\nAccept the invite: [cyan]{cfg.INVITE_URL}[/]",
            title="action required", border_style="yellow"))

    if dr.get("draft_order"):
        smap = sleeper.draft_slot_map(cfg.DRAFT_ID)
        mine = next((s for s, u in smap.items() if u == cfg.MY_USER_ID), None)
        if mine and mine != cfg.MY_DRAFT_SLOT:
            con.print(f"[red]SLOT MISMATCH:[/] Sleeper has you at slot {mine}, config says {cfg.MY_DRAFT_SLOT}. Update config.MY_DRAFT_SLOT.")
        elif mine:
            con.print(f"[green]draft slot confirmed: {mine}[/]")

    board, diag = get_board(refresh)
    con.print(f"\nboard: {diag['board_size']} players | ADP matched {diag['ffc']['matched']}/{diag['ffc']['entries']} "
              f"| ECR {diag['ecr_matched']} ({diag['ecr']['experts']} experts, updated {diag['ecr']['updated']})")


@app.command()
def board(
    position: str = typer.Option(None, "--pos", "-p"),
    limit: int = typer.Option(40, "--limit", "-n"),
    sort: str = typer.Option("vor", help="vor | adp | value"),
):
    """Show the valued draft board."""
    b, _ = get_board()
    rows = [e for e in b if not position or e.position == position.upper()]
    if sort == "adp":
        rows.sort(key=lambda e: e.adp)
    elif sort == "value":
        rows.sort(key=lambda e: -e.adp_value)
    rows = rows[:limit]

    t = Table(title=f"Draft board ({sort})")
    for c, j in [("#","right"),("Player","left"),("Pos","left"),("Team","left"),("Pts","right"),
                 ("VOR","right"),("T","right"),("ADP","right"),("ECR","right"),("Gap","right"),("Bye","right"),("Inj","left")]:
        t.add_column(c, justify=j)
    for e in rows:
        gap = e.ecr_adp_gap
        t.add_row(str(e.valuation.vor_rank), e.name, e.position, e.team or "-",
                  f"{e.points:.0f}", f"{e.vor:.0f}", str(e.tier), f"{e.adp:.1f}",
                  str(e.ecr_rank or "-"), (f"{gap:+.0f}" if gap is not None else "-"),
                  str(e.bye or "-"), e.injury_status or "")
    con.print(t)


@app.command()
def values(limit: int = typer.Option(25, "--limit", "-n")):
    """Biggest gaps between what a player is worth and where the room drafts him."""
    b, _ = get_board()
    # Restrict to players who would actually crack a starting lineup. ECR
    # covers 515 players vs. ADP's 268, so deep-tail players show huge
    # spurious gaps that reflect differing coverage, not mispricing.
    pool = [e for e in b if e.adp < 130 and e.ecr_adp_gap is not None
            and e.valuation.vor_rank <= 140]
    pool.sort(key=lambda e: -(e.ecr_adp_gap or 0))
    t = Table(title="Market inefficiencies (experts vs. the room)")
    for c in ("Player","Pos","ADP","ECR","Gap","VOR rank","Tier","Bye"):
        t.add_column(c)
    for e in pool[:limit]:
        t.add_row(e.name, e.position, f"{e.adp:.1f}", str(e.ecr_rank),
                  f"{e.ecr_adp_gap:+.0f}", str(e.valuation.vor_rank), str(e.tier), str(e.bye or "-"))
    con.print(t)
    con.print("\n[dim]Positive gap = experts rank him higher than the room drafts him -> buy low.[/]")


@app.command()
def live(
    poll: float = typer.Option(2.0, help="seconds between polls"),
    sims: int = typer.Option(500, help="simulations per decision"),
):
    """Watch the live draft and recommend a pick the moment it's our turn."""
    brain = get_brain()
    brain.n_sims = sims
    ld = LiveDraft(brain=brain, poll_seconds=poll)

    slot = ld.resolve_slot()
    if slot and slot != cfg.MY_DRAFT_SLOT:
        con.print(f"[yellow]Sleeper says our slot is {slot}, config said {cfg.MY_DRAFT_SLOT}. Using {slot}.[/]")
        ld.my_slot = slot
        ld.state.my_slot = slot
        brain.my_slot = slot

    con.print(Panel(f"watching draft [cyan]{cfg.DRAFT_ID}[/] | slot {ld.my_slot} | "
                    f"picks {cfg.my_picks(ld.my_slot)[:5]}...", title="live draft"))
    last_shown = -1
    while True:
        try:
            new, rec = ld.poll_once()
            if new:
                for line in ld.pick_feed(new):
                    con.print(f"  [dim]{line}[/]")
            if rec and rec.pick_no != last_shown:
                last_shown = rec.pick_no
                body = f"[bold green]{rec.name}[/]  ({rec.primary.position})\n\n"
                body += "\n".join(f"• {r}" for r in rec.reasoning)
                body += "\n\n[dim]alternatives: " + ", ".join(
                    f"{a.name} ({a.win_rate:.0%})" for a in rec.alternatives[:4]) + "[/]"
                con.print(Panel(body, title=f"YOUR PICK — #{rec.pick_no} (round {rec.round_})",
                                border_style="green"))
            if ld.state.picks_made >= cfg.NUM_TEAMS * cfg.DRAFT_ROUNDS:
                con.print("[green]draft complete[/]")
                break
            time.sleep(poll)
        except KeyboardInterrupt:
            con.print("\nstopped")
            break
        except Exception as exc:  # keep the loop alive during a draft
            con.print(f"[red]poll error:[/] {exc}")
            time.sleep(poll * 2)


@app.command()
def mock(
    rounds: int = typer.Option(cfg.DRAFT_ROUNDS),
    sims: int = typer.Option(300),
    seed: int = typer.Option(2026, help="vary this to rehearse different draft rooms"),
):
    """Run a full mock draft against simulated opponents to test the engine."""
    import numpy as np
    brain = get_brain()
    brain.n_sims = sims
    st = DraftState(my_slot=cfg.MY_DRAFT_SLOT)
    players = brain.players
    rng = np.random.default_rng(seed)
    adp = np.array([p.adp for p in players]); sd = np.array([p.adp_sd for p in players])
    order = list(np.argsort(adp + rng.standard_normal(len(players)) * sd))

    total = cfg.NUM_TEAMS * rounds
    caps: dict[int, dict[str, int]] = {}
    from .draft.simulate import OPPONENT_CAPS
    picks: list[dict] = []
    for pick in range(1, total + 1):
        slot = cfg.slot_for_pick(pick)
        if slot == cfg.MY_DRAFT_SLOT:
            rec = brain.recommend(st, n_sims=sims)
            if not rec.primary:
                break
            pid = rec.primary.player_id
            con.print(f"[green]R{st.current_round:>2} #{pick:>3}[/]  we take [bold]{rec.primary.name}[/] "
                      f"({rec.primary.position})  conf {rec.confidence:.0%}  +{rec.margin}")
        else:
            oc = caps.setdefault(slot, {})
            pid = None
            for i in order:
                p = players[i]
                if p.player_id in st.drafted: continue
                if oc.get(p.position, 0) >= OPPONENT_CAPS.get(p.position, 99): continue
                pid = p.player_id; oc[p.position] = oc.get(p.position, 0) + 1; break
            if pid is None: break
        pos = brain.entries[pid].position
        picks.append({"pick_no": pick, "player_id": pid, "draft_slot": slot, "metadata": {"position": pos}})
        st.apply_picks(picks, {e.player_id: e.position for e in brain.board})

    con.print()
    t = Table(title="our mock roster")
    for c in ("Pos","Player","Team","Proj","ADP","Bye"): t.add_column(c)
    mine = [brain.entries[p] for p in st.me.players]
    for e in sorted(mine, key=lambda e: (-e.points)):
        t.add_row(e.position, e.name, e.team or "-", f"{e.points:.0f}", f"{e.adp:.1f}", str(e.bye or "-"))
    con.print(t)

    from .model.roster import RosterPlayer, optimal_lineup, season_value
    from .draft.simulate import availability_for
    rp = [RosterPlayer(e.player_id, e.position, e.points / max(e.games,1), e.bye,
                       availability_for(e.position, e.injury_status)) for e in mine]
    pts, lineup = optimal_lineup(rp)
    con.print(f"\noptimal weekly lineup: [bold]{pts:.1f} ppg[/]  |  "
              f"projected season: [bold]{season_value(rp, replacement=brain.replacement):.0f}[/] pts")
    names = {e.player_id: e.name for e in mine}
    for slot_name, ids in lineup.items():
        con.print(f"   {slot_name:<5} " + ", ".join(names.get(i, i) for i in ids))


@app.command()
def plan(sims: int = typer.Option(600), out: str = typer.Option(None, "--out")):
    """Pre-draft plan: what to do at each of our picks, under realistic boards."""
    import numpy as np
    from .draft.simulate import simulate_candidates

    brain = get_brain()
    players = brain.players
    b_entries, _ = get_board()
    by_adp = sorted(players, key=lambda p: p.adp)
    lines: list[str] = []

    def emit(txt=""):
        lines.append(txt)
        con.print(txt)

    emit(f"[bold]Draft plan — slot {cfg.MY_DRAFT_SLOT} of {cfg.NUM_TEAMS}, "
         f"picks {', '.join('#'+str(p) for p in cfg.my_picks()[:6])}...[/]")
    emit(f"waiver/stream replacement ppg: {brain.replacement}")
    emit()

    # Round 1 scenarios are DERIVED from live ADP, not hardcoded names, so
    # this command stays correct as camp news moves the board over the next
    # three weeks. Each scenario is a world where one top-seven player slips
    # to our pick and the five cheapest of the rest are gone.
    base = by_adp[:7]
    scenarios: dict[str, list[str]] = {}
    seen_worlds: set[frozenset] = set()
    for x in base[2:7]:
        gone = [p.name for p in base if p.player_id != x.player_id][:5]
        world = frozenset(gone)
        # The last two candidates produce the same world (top five gone) --
        # one sim, one honest label.
        if world in seen_worlds:
            continue
        seen_worlds.add(world)
        avail = [p.name for p in base if p.name not in world]
        label = " / ".join(n.split()[-1] for n in avail[:2]) + " on the board"
        scenarios[label] = gone
    emit("[bold]PICK #6 — decision table[/]")
    for label, gone_names in scenarios.items():
        gone = {p.player_id for p in players if p.name in gone_names}
        cands = [p for p in by_adp[:15] if p.player_id not in gone][:6]
        res = simulate_candidates(players=players, taken=gone, my_roster_ids=[],
                                  current_pick=6, candidates=cands, n_sims=sims,
                                  my_slot=cfg.MY_DRAFT_SLOT, replacement=brain.replacement)
        top = res[0]
        runner = res[1] if len(res) > 1 else None
        emit(f"  if gone = {', '.join(n.split()[-1] for n in gone_names)}")
        emit(f"     -> [green]{top.name}[/] ({top.position}), {top.win_rate:.0%} confidence"
             + (f", +{top.mean_value-runner.mean_value:.1f} over {runner.name}" if runner else ""))
    emit()

    # Who realistically reaches each of our later picks.
    #
    # Walked forward through a real draft -- we take the engine's own pick each
    # round, so every earlier selection (ours included) is genuinely off the
    # board. Measuring availability against an empty board instead would count
    # the first-round studs as still available and wildly overstate who reaches
    # us. Probabilities come from the same opponent model that makes the pick.
    emit("[bold]LATER PICKS — who realistically reaches us[/]")
    import numpy as np
    from .draft.simulate import OPPONENT_CAPS, empirical_survival

    st = DraftState(my_slot=cfg.MY_DRAFT_SLOT)
    made: list[dict] = []
    rng = np.random.default_rng(31)
    a = np.array([p.adp for p in players]); sdv = np.array([p.adp_sd for p in players])
    order = list(np.argsort(a + rng.standard_normal(len(players)) * sdv))
    caps: dict[int, dict] = {}
    posmap = {e.player_id: e.position for e in b_entries}

    for pick in range(1, cfg.NUM_TEAMS * 9 + 1):
        slot = cfg.slot_for_pick(pick)
        if slot == cfg.MY_DRAFT_SLOT:
            nxt = st.next_pick_after(pick)
            if nxt and pick > cfg.my_picks()[0]:
                surv = empirical_survival(players, set(st.drafted), pick, nxt, n_sims=400)
                avail = sorted((p for p in players if p.player_id not in st.drafted),
                               key=lambda p: -brain.entries[p.player_id].vor)
                shown = [p for p in avail[:60] if 0.25 <= surv[p.player_id] <= 0.95][:4]
                if shown:
                    emit(f"  #{pick:>3} (round {(pick-1)//cfg.NUM_TEAMS+1:>2}): " +
                         ", ".join(f"{p.name} ({p.position}, {surv[p.player_id]:.0%})" for p in shown))
            rec = brain.recommend(st, n_sims=max(sims // 3, 80))
            if not rec.primary:
                break
            pid = rec.primary.player_id
        else:
            oc = caps.setdefault(slot, {})
            pid = None
            for i in order:
                q = players[i]
                if q.player_id in st.drafted: continue
                if oc.get(q.position, 0) >= OPPONENT_CAPS.get(q.position, 99): continue
                oc[q.position] = oc.get(q.position, 0) + 1; pid = q.player_id; break
            if pid is None:
                break
        made.append({"pick_no": pick, "player_id": pid, "draft_slot": slot,
                     "metadata": {"position": posmap[pid]}})
        st.apply_picks(made, posmap)
    emit()

    b, _ = get_board()
    vals = sorted([e for e in b if e.adp < 130 and e.ecr_adp_gap
                   and e.valuation.vor_rank <= 140],
                  key=lambda e: -e.ecr_adp_gap)[:10]
    emit("[bold]TARGETS — experts rank them well above where the room drafts them[/]")
    for e in vals:
        emit(f"  {e.name:<24} {e.position:<3} ADP {e.adp:>5.1f}  ECR {e.ecr_rank:>3}  gap {e.ecr_adp_gap:+.0f}  tier {e.tier}")

    if out:
        import re
        pathlib_out = __import__("pathlib").Path(out)
        pathlib_out.write_text("\n".join(re.sub(r"\[/?[a-z ]+\]", "", l) for l in lines))
        con.print(f"\n[dim]written to {out}[/]")


@app.command()
def lineup(week: int = typer.Option(None), opponent_total: float = typer.Option(None)):
    """Optimal starting lineup for a week, from live weekly projections."""
    from .data import projections as pj
    from .season.lineup import Candidate, optimize_points, optimize_win_probability, lineup_stats

    wk = week or sleeper.active_week()
    rosters = sleeper.rosters(cfg.LEAGUE_ID, fresh=True)
    mine = next((r for r in rosters if r.get("owner_id") == cfg.MY_USER_ID), None)
    if not mine or not mine.get("players"):
        con.print("[yellow]no roster yet — draft hasn't happened.[/]"); raise typer.Exit()

    weekly = pj.weekly_projections(week=wk)
    b, _ = get_board(); ent = {e.player_id: e for e in b}
    cands = []
    for pid in mine["players"]:
        w = weekly.get(pid); e = ent.get(pid)
        if not e: continue
        cands.append(Candidate(pid, e.name, e.position, w.points if w else 0.0,
                               e.injury_status, is_bye=(e.bye == wk)))
    lu, total = optimize_points(cands)
    _, sd = lineup_stats(lu)
    con.print(f"[bold]Week {wk} lineup[/] — projected {total:.1f} pts (sd {sd:.1f})")
    for slot, group in lu.items():
        con.print(f"  {slot:<5} " + ", ".join(f"{c.name} ({c.proj:.1f})" for c in group))
    if opponent_total:
        lu2, t2, wp = optimize_win_probability(cands, opponent_total, sd)
        con.print(f"\n[bold]vs opponent projected {opponent_total:.1f}[/] — win prob {wp:.1%}")
        if abs(t2 - total) > 0.05:
            con.print(f"  variance-adjusted lineup ({t2:.1f} pts) beats the points-max one on win probability:")
            for slot, group in lu2.items():
                con.print(f"    {slot:<5} " + ", ".join(c.name for c in group))


@app.command()
def stream(position: str = typer.Argument("DEF"), week: int = typer.Option(None)):
    """Best streaming options at K / DEF / QB for a week."""
    from .season.streaming import weekly_options
    wk = week or sleeper.active_week()
    rostered: set[str] = set()
    for r in sleeper.rosters(cfg.LEAGUE_ID):
        rostered |= set(r.get("players") or [])
    opts = weekly_options(position.upper(), wk, exclude=rostered)
    t = Table(title=f"Week {wk} {position.upper()} streamers (free agents only)")
    for c in ("Player","Team","Proj"): t.add_column(c)
    for o in opts: t.add_row(o.name, o.team or "-", f"{o.proj:.1f}")
    con.print(t)


def _form(board) -> dict[str, float]:
    """player_id -> what he is worth per game NOW (season/form.py), cached.
    Before week 2 there is nothing to blend and this is the preseason ppg."""
    if "form" not in _cache:
        from .season.form import form_table
        _cache["form"] = form_table(board, sleeper.active_week(), cfg.SEASON)
    return _cache["form"]


def _roster_player(e, form: dict[str, float]):
    from .draft.simulate import availability_for
    from .model.roster import RosterPlayer
    return RosterPlayer(e.player_id, e.position,
                        form.get(e.player_id, e.points / max(e.games, 1)),
                        e.bye, availability_for(e.position, e.injury_status))


def _my_roster_players(board):
    """Our roster as RosterPlayer objects, or None before the draft."""
    rosters = sleeper.rosters(cfg.LEAGUE_ID, fresh=True)
    mine = next((r for r in rosters if r.get("owner_id") == cfg.MY_USER_ID), None)
    if not mine or not mine.get("players"):
        return None, rosters
    ent = {e.player_id: e for e in board}
    form = _form(board)
    return [_roster_player(ent[pid], form) for pid in mine["players"] if pid in ent], rosters


def _faab_left() -> int:
    """FAAB dollars actually remaining, read from Sleeper rather than assumed."""
    rosters = sleeper.rosters(cfg.LEAGUE_ID, fresh=True)
    mine = next((r for r in rosters if r.get("owner_id") == cfg.MY_USER_ID), None)
    used = int(((mine or {}).get("settings") or {}).get("waiver_budget_used") or 0)
    return max(cfg.FAAB_BUDGET - used, 0)


def _waiver_targets(budget: int, top_pool: int = 80):
    """Rank free agents by what they add to OUR lineup. Shared by the report
    (`waivers`) and the executor (`execute-waivers`) so they cannot disagree."""
    from .model.roster import expected_season_value, optimal_lineup
    from .season.waivers import WaiverTarget, rank_targets, weeks_remaining

    board, _ = get_board()
    brain = get_brain()
    mine, rosters = _my_roster_players(board)
    if mine is None:
        return None

    rostered: set[str] = set()
    for r in rosters:
        rostered |= set(r.get("players") or [])

    wk = sleeper.active_week()
    left = weeks_remaining(wk)
    trend = {t["player_id"]: t.get("count", 0) for t in sleeper.trending("add", limit=200)}
    base = expected_season_value(mine, weeks=left, replacement=brain.replacement)

    # Value everyone at current form, not the August projection: the pool is
    # ordered by it, so a breakout the preseason board had at 1.5 ppg is seen.
    form = _form(board)
    ent = {e.player_id: e for e in board}
    targets: list[WaiverTarget] = []
    pool = sorted((e for e in board if e.player_id not in rostered and form.get(e.player_id, 0) > 0),
                  key=lambda e: -form[e.player_id])[:top_pool]
    for e in pool:
        cand = _roster_player(e, form)
        after = expected_season_value(mine + [cand], weeks=left,
                                      replacement=brain.replacement)
        marginal = (after - base) / max(left, 1)       # upper bound: no drop yet
        if marginal <= 0.01:
            continue
        targets.append(WaiverTarget(
            player_id=e.player_id, name=e.name, position=e.position, team=e.team,
            proj_ppg=form[e.player_id], marginal_ppg=marginal,
            weeks_left=left, trending_adds=trend.get(e.player_id, 0)))

    # The roster is full, so every add costs a drop. Re-price the best few net
    # of the cheapest drop among our weakest non-starters; a starter is never
    # cut. Without this the gain is overstated by whatever the dropped player
    # was worth as depth.
    full = len(mine) >= len(cfg.ROSTER_POSITIONS)
    targets.sort(key=lambda t: -t.marginal_ppg)
    for t in targets[:8] if full else []:
        cand = _roster_player(ent[t.player_id], form)
        # Who is on the bench once HE is here: adding a defense benches the
        # old one, and the old one -- not a receiver -- is the natural drop.
        _, lu = optimal_lineup(mine + [cand], replacement=brain.replacement)
        starting = {pid for ids in lu.values() for pid in ids}
        droppable = sorted((p for p in mine if p.player_id not in starting),
                           key=lambda p: p.ppg * p.avail)[:5]
        if not droppable:
            t.marginal_ppg = 0.0
            continue
        best = None
        for d in droppable:
            kept = [p for p in mine if p.player_id != d.player_id]
            net = (expected_season_value(kept + [cand], weeks=left,
                                         replacement=brain.replacement) - base) / max(left, 1)
            if best is None or net > best[0]:
                best = (net, d)
        t.marginal_ppg, t.drop_id = best[0], best[1].player_id
        t.drop_name = ent[best[1].player_id].name
    if full:
        targets = [t for t in targets[:8] if t.marginal_ppg > 0.01]

    rank_targets(targets, budget_left=budget)
    return {"targets": targets, "mine": mine, "board": board, "brain": brain,
            "week": wk, "left": left}


@app.command()
def waivers(
    budget: int = typer.Option(None, help="FAAB dollars remaining (default: read from Sleeper)"),
    limit: int = typer.Option(15, "--limit", "-n"),
):
    """Free-agent targets, ranked by what they add to OUR lineup, with FAAB bids."""
    budget = _faab_left() if budget is None else budget
    res = _waiver_targets(budget)
    if res is None:
        con.print("[yellow]no roster yet — the draft hasn't happened.[/]")
        raise typer.Exit()
    targets, wk, left = res["targets"], res["week"], res["left"]

    t = Table(title=f"Week {wk} waiver targets — ${budget} FAAB left, {left} weeks remaining")
    for c, j in [("Player","left"),("Pos","left"),("Tm","left"),("Proj","right"),
                 ("Adds to us","right"),("Bid","right"),("Why","left")]:
        t.add_column(c, justify=j)
    for w in targets[:limit]:
        t.add_row(w.name, w.position, w.team or "-", f"{w.proj_ppg:.1f}",
                  f"+{w.marginal_ppg:.2f}", f"${w.bid_dollars}", w.rationale[:52])
    con.print(t)
    if not targets:
        con.print("[dim]nobody on waivers improves the starting lineup — stand pat.[/]")


@app.command("execute-waivers")
def execute_waivers(
    live: bool = typer.Option(False, "--live", help="actually submit on Sleeper (default is a dry run)"),
    min_gain: float = typer.Option(0.5, help="an add must lift our lineup by at least this many ppg"),
    max_moves: int = typer.Option(1, help="moves per run"),
    headless: bool = typer.Option(True),
):
    """Submit the best waiver move on Sleeper. Dry run unless --live.

    The drop is always the lowest-value player NOT in our optimal starting
    lineup, so a starter is never cut to make room. Every move is logged and
    notified; a claim on a player still on waivers is held by Sleeper until
    Wednesday and can be cancelled from the Waivers panel until then.
    """
    from .model.roster import optimal_lineup
    from .season.executor import add_player

    budget = _faab_left()
    res = _waiver_targets(budget)
    if res is None:
        con.print("[yellow]no roster on Sleeper yet[/]")
        raise typer.Exit()

    # Net gain (after the drop) is the only test. A $0 bid is legal and wins
    # any unclaimed free agent, so it must not disqualify a move.
    picks = [t for t in res["targets"] if t.marginal_ppg >= min_gain][:max_moves]
    con.print(f"[bold]week {res['week']}[/] — ${budget} FAAB left, threshold +{min_gain} ppg")
    if not picks:
        con.print("[green]no add clears the bar — standing pat.[/]")
        return

    mine, board = res["mine"], res["board"]
    ent = {e.player_id: e for e in board}
    _, lu = optimal_lineup(mine, replacement=res["brain"].replacement)
    starting = {pid for ids in lu.values() for pid in ids}
    bench = sorted((p for p in mine if p.player_id not in starting), key=lambda p: p.ppg)

    for t in picks:
        if t.drop_name:                       # the drop the valuation priced in
            drop_name = t.drop_name
            bench = [p for p in bench if p.player_id != t.drop_id]
        else:
            drop = bench.pop(0) if bench else None
            drop_name = ent[drop.player_id].name if drop else None
        con.print(f"{'[yellow]DRY RUN[/] ' if not live else ''}add [bold]{t.name}[/] ({t.position}) "
                  f"bid ${t.bid_dollars}, +{t.marginal_ppg:.2f} ppg"
                  + (f", drop {drop_name}" if drop_name else ""))
        rep = add_player(t.name, drop_name=drop_name, bid=t.bid_dollars,
                         dry_run=not live, headless=headless)
        con.print(f"   -> {rep['status']}: {rep['detail']}")


@app.command()
def trade(roster_id: int = typer.Argument(..., help="the other team's roster_id"),
          limit: int = typer.Option(6, "--limit", "-n")):
    """Find swaps with another team that improve BOTH rosters."""
    from .draft.simulate import availability_for
    from .model.roster import RosterPlayer
    from .season.trades import find_mutual_trades

    board, _ = get_board()
    brain = get_brain()
    ent = {e.player_id: e for e in board}
    mine, rosters = _my_roster_players(board)
    if mine is None:
        con.print("[yellow]no roster yet — the draft hasn't happened.[/]"); raise typer.Exit()

    other = next((r for r in rosters if r.get("roster_id") == roster_id), None)
    if not other or not other.get("players"):
        con.print(f"[red]roster {roster_id} has no players[/]"); raise typer.Exit()
    theirs = []
    for pid in other["players"]:
        e = ent.get(pid)
        if e:
            theirs.append(_roster_player(e, _form(board)))   # same yardstick as ours

    from .season.waivers import weeks_remaining
    left = weeks_remaining(sleeper.active_week())
    deals = find_mutual_trades(mine, theirs, replacement=brain.replacement,
                               max_each=2, top_n=limit, weeks=left)
    if not deals:
        con.print("[dim]no mutually beneficial swap found with that roster.[/]"); raise typer.Exit()

    nm = {e.player_id: e.name for e in board}
    t = Table(title=f"Mutual-gain trades with roster {roster_id}")
    for c in ("We send","We get","Our gain","Their gain"): t.add_column(c)
    for send, recv, ev in deals:
        t.add_row(", ".join(nm.get(i,i) for i in send), ", ".join(nm.get(i,i) for i in recv),
                  f"+{ev.our_gain:.0f}", f"+{ev.their_gain:.0f}")
    con.print(t)
    con.print("\n[dim]Both columns positive means they have a real reason to accept.[/]")


@app.command()
def queue(depth: int = typer.Option(40, help="how many players to queue"),
          out: str = typer.Option("docs/draft-queue.txt", "--out")):
    """Export a pre-draft queue to enter into Sleeper by hand.

    Insurance for draft day. Sleeper's autopick follows YOUR QUEUE before it
    falls back to its own ranking -- and its own ranking is unusable here: it
    is a search-popularity index that over-ranks quarterbacks and carries
    retired players inside its top 100. Entering this queue before the draft
    means that if you lose signal, fall asleep, or the engine dies, the
    autopicker still takes AI-chosen players in AI-chosen order.
    """
    board, _ = get_board()
    brain = get_brain()

    from .draft.queueing import build_queue
    picked = build_queue(board, depth=depth)

    t = Table(title=f"Pre-draft queue — enter these into Sleeper in this order")
    for c, j in [("#","right"),("Player","left"),("Pos","left"),("Tm","left"),
                 ("ADP","right"),("VOR","right"),("Tier","right"),("Bye","right")]:
        t.add_column(c, justify=j)
    for i, e in enumerate(picked, 1):
        t.add_row(str(i), e.name, e.position, e.team or "-", f"{e.adp:.1f}",
                  f"{e.vor:.0f}", str(e.tier), str(e.bye or "-"))
    con.print(t)

    lines = [f"{i:>3}. {e.name}  ({e.position} {e.team or '--'})  ADP {e.adp:.1f}  bye {e.bye or '-'}"
             for i, e in enumerate(picked, 1)]
    header = [
        f"{cfg.LEAGUE_NAME.upper()} - PRE-DRAFT QUEUE",
        f"slot {cfg.MY_DRAFT_SLOT} of {cfg.NUM_TEAMS} | picks " +
        ", ".join(f"#{p}" for p in cfg.my_picks()[:6]) + ", ...",
        "Enter in this order in Sleeper's draft queue BEFORE the draft starts.",
        "Autopick follows the queue before its own (unusable) default ranking.",
        "",
    ]
    pth = __import__("pathlib").Path(out)
    pth.parent.mkdir(parents=True, exist_ok=True)
    pth.write_text("\n".join(header + lines) + "\n")
    con.print(f"\n[dim]written to {out} — enter these into Sleeper before draft day[/]")


@app.command()
def preflight():
    """Draft-morning checklist: verify every system this draft depends on.

    Run this the morning of the draft. Anything below PASS needs attention
    before the clock starts.
    """
    import time as _time
    from datetime import date, datetime

    rows: list[tuple[str, str, str]] = []   # (status, check, detail)

    def check(name):
        def deco(fn):
            try:
                status, detail = fn()
            except Exception as exc:
                status, detail = "FAIL", f"{type(exc).__name__}: {exc}"
            rows.append((status, name, detail))
        return deco

    @check("In-season automation")
    def _():
        """The engine should be the login service; a window running it is fragile."""
        import fcntl, os
        from . import runner as rn
        window_running = False
        try:
            fh = open(rn.LOCK_FILE, "a")
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh, fcntl.LOCK_UN)       # we got it -> nobody holds it
            except BlockingIOError:
                window_running = True
            fh.close()
        except OSError:
            pass
        try:
            import playwright  # noqa: F401
            pw_ok = True
        except ImportError:
            pw_ok = False
        signed = os.path.exists(os.path.expanduser("~/.mishpacha-browser/.logged_in"))
        problems = []
        if not pw_ok:
            problems.append("playwright not installed (uv pip install -e .)")
        if not signed:
            problems.append("not signed in (mish login)")
        if problems:
            return "FAIL", "; ".join(problems)
        if rn.engine_alive() and rn.engine_loaded():
            return "PASS", "engine running as login service, browser signed in, executor ready"
        if rn.engine_alive():
            return "WARN", "engine running, but started by hand -- run `mish install` so it survives logout/reboot"
        if rn.engine_loaded():
            return "WARN", "login service loaded but engine not reporting -- see logs/daemon.err"
        if window_running:
            return "WARN", "engine runs only inside an open window -- run `mish install` so closing it can't stop the season"
        return "FAIL", "nothing is running -- run `mish install` (or open Mishpacha.command)"

    @check("Sleeper API reachable")
    def _():
        t = _time.time()
        st = sleeper.nfl_state()
        return "PASS", f"season {st['season']} ({st['season_type']}), {(_time.time()-t)*1000:.0f}ms"

    @check("League membership")
    def _():
        users = sleeper.league_users(cfg.LEAGUE_ID)
        rs = sleeper.rosters(cfg.LEAGUE_ID)
        owned = [r for r in rs if r.get("owner_id")]
        rid = sleeper.my_roster_id(cfg.LEAGUE_ID, cfg.MY_USER_ID)
        if rid is None:
            return "FAIL", f"you own no roster — accept {cfg.INVITE_URL}"
        # More user accounts than rosters is normal: co-managers share a team.
        extra = len(users) - len(rs)
        note = f", {extra} co-manager account(s)" if extra > 0 else ""
        if len(owned) == cfg.NUM_TEAMS:
            return "PASS", f"{len(owned)}/{cfg.NUM_TEAMS} rosters claimed{note}; you are roster {rid}"

        # An unclaimed roster still drafts -- Sleeper autopicks for it. That is
        # worth naming precisely, because which SLOT it sits in decides how many
        # of the picks between ours are made by a bot rather than a person.
        orphan_ids = {r["roster_id"] for r in rs if not r.get("owner_id")}
        try:
            s2r = sleeper.slot_to_roster(cfg.DRAFT_ID, ttl=0)
        except Exception:
            s2r = {}
        slots = sorted(sl for sl, r in s2r.items() if r in orphan_ids)
        where = f" at draft slot(s) {slots}" if slots else ""
        return "WARN", (f"{len(owned)}/{cfg.NUM_TEAMS} rosters claimed{note}; you are roster {rid}. "
                        f"Roster(s) {sorted(orphan_ids)} unclaimed{where} — they will AUTOPICK "
                        f"the whole draft unless someone takes the seat.")

    @check("Draft order complete")
    def _():
        dr = sleeper.draft(cfg.DRAFT_ID, ttl=0)
        order = dr.get("draft_order") or {}
        s2r = {int(k): v for k, v in (dr.get("slot_to_roster_id") or {}).items()}
        if not order and not s2r:
            return "WARN", "commissioner has not set the order yet"
        gaps = sorted(set(range(1, cfg.NUM_TEAMS + 1)) - {int(v) for v in order.values()})
        if gaps:
            return "WARN", (f"draft_order is missing slot(s) {gaps} ({len(order)}/"
                            f"{cfg.NUM_TEAMS} assigned) — ask the commissioner to finish it; "
                            f"slot_to_roster_id is complete, so the draft itself will run")
        return "PASS", f"all {cfg.NUM_TEAMS} slots assigned"

    @check("Draft scheduled")
    def _():
        dr = sleeper.draft(cfg.DRAFT_ID, ttl=0)
        ts = dr.get("start_time")
        if not ts:
            days = (cfg.LAST_SAFE_DRAFT_DAY - date.today()).days
            return "WARN", (f"start_time not set on Sleeper — {days} days left to schedule "
                            f"before the {cfg.LAST_SAFE_DRAFT_DAY:%b %d} deadline")
        when = datetime.fromtimestamp(ts / 1000)
        return "PASS", f"{when:%A %b %d, %I:%M %p} (status: {dr['status']})"

    @check("Draft beats Week 1 kickoff")
    def _():
        """A draft on or after kickoff costs the league a scoring week."""
        dr = sleeper.draft(cfg.DRAFT_ID, ttl=0)
        ts = dr.get("start_time")
        target = f"target {cfg.TARGET_DRAFT_WINDOW[0]:%b %d}-{cfg.TARGET_DRAFT_WINDOW[1]:%b %d}"
        if not ts:
            return "WARN", (f"unscheduled. Week 1 opens {cfg.WEEK1_KICKOFF:%a %b %d}; "
                            f"last safe day {cfg.LAST_SAFE_DRAFT_DAY:%a %b %d} ({target})")
        d = datetime.fromtimestamp(ts / 1000).date()
        if d >= cfg.WEEK1_KICKOFF:
            return "FAIL", (f"draft is {d:%b %d}, on/after the {cfg.WEEK1_KICKOFF:%b %d} "
                            f"kickoff — the league loses a scoring week. Move it earlier.")
        slack = (cfg.WEEK1_KICKOFF - d).days
        status = "PASS" if slack >= 1 else "WARN"
        return status, f"{d:%a %b %d} — {slack} day(s) before kickoff"

    @check("Draft slot binding")
    def _():
        from .live import LiveDraft
        ld = LiveDraft(brain=get_brain())
        mine = ld.resolve_slot()
        if mine is None:
            return "WARN", (f"order not set — assuming slot {cfg.MY_DRAFT_SLOT} from the chat post")
        if ld.slot_conflict:
            a, b = ld.slot_conflict
            return "FAIL", (f"Sleeper disagrees with itself: slot_to_roster_id says {a}, "
                            f"draft_order says {b}. Ask the commissioner to re-save the order.")
        if mine != cfg.MY_DRAFT_SLOT:
            return "FAIL", (f"Sleeper slot {mine} != config slot {cfg.MY_DRAFT_SLOT} — "
                            f"set config.MY_DRAFT_SLOT = {mine}")
        return "PASS", f"slot {mine} confirmed by both Sleeper mappings"

    @check("Board build + data freshness")
    def _():
        board, diag = get_board(refresh=True)
        ffc = diag["ffc"]
        bad = ffc["entries"] - ffc["matched"]
        window = ffc["meta"].get("end_date", "?")
        det = (f"{diag['board_size']} players, ADP {ffc['matched']}/{ffc['entries']} "
               f"(window ends {window}), ECR {diag['ecr_matched']} matched (updated {diag['ecr']['updated']})")
        return ("PASS" if bad == 0 else "WARN"), det

    @check("Decision engine speed")
    def _():
        from .draft.state import DraftState
        brain = get_brain()
        t = _time.time()
        rec = brain.recommend(DraftState(my_slot=cfg.MY_DRAFT_SLOT), width=4, n_sims=150)
        dt = _time.time() - t
        if not rec.primary:
            return "FAIL", "engine returned no recommendation"
        status = "PASS" if dt < 15 else "WARN"
        return status, f"{dt:.1f}s for a full decision (clock is {cfg.PICK_TIMER_SECONDS}s) — would take {rec.name}"

    @check("Fallback queue file")
    def _():
        import os
        pth = "docs/draft-queue.txt"
        try:
            if sleeper.draft(cfg.DRAFT_ID).get("status") == "complete":
                return "PASS", "draft is over — queue file no longer matters"
        except Exception:
            pass
        if not os.path.exists(pth):
            return "WARN", "not generated — run `mish queue` and enter it into Sleeper"
        age_h = (_time.time() - os.path.getmtime(pth)) / 3600
        status = "PASS" if age_h < 48 else "WARN"
        return status, f"{pth} written {age_h:.0f}h ago" + ("" if age_h < 48 else " — regenerate, ADP has moved")

    colors = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}
    t = Table(title="Draft-day preflight")
    t.add_column("")
    t.add_column("Check")
    t.add_column("Detail", overflow="fold")
    for status, name, detail in rows:
        t.add_row(f"[{colors[status]}]{status}[/]", name, detail)
    con.print(t)
    fails = sum(1 for s_, _, _ in rows if s_ == "FAIL")
    warns = sum(1 for s_, _, _ in rows if s_ == "WARN")
    if fails:
        con.print(f"[red]{fails} failure(s) — do not draft until these are fixed.[/]")
    elif warns:
        con.print(f"[yellow]{warns} warning(s) — review before the clock starts.[/]")
    else:
        con.print("[green]all systems go.[/]")


@app.command()
def serve(
    port: int = typer.Option(8777, help="port to listen on"),
    host: str = typer.Option("0.0.0.0", help="0.0.0.0 exposes it to your phone on the same wifi"),
    sims: int = typer.Option(500),
):
    """Start the live draft dashboard. Open it on your phone during the draft."""
    import socket
    import uvicorn
    from .server import app as web, engine

    engine.sims = sims
    try:
        lan = socket.gethostbyname(socket.gethostname())
    except Exception:
        lan = "localhost"
    con.print(Panel(
        f"[bold]http://localhost:{port}[/]\n"
        f"on your phone (same wifi): [bold]http://{lan}:{port}[/]\n\n"
        f"[dim]the board takes ~10s to warm up, then it polls every 2s[/]",
        title="draft dashboard", border_style="green"))
    uvicorn.run(web, host=host, port=port, log_level="warning")


@app.command()
def run(
    check: bool = typer.Option(False, "--check", help="print the schedule and exit"),
    daemon: bool = typer.Option(False, "--daemon", help="run the engine headless (used by the login service)"),
):
    """The one program: live season dashboard; the engine runs every job."""
    from .runner import main
    main(check=check, daemon=daemon)


@app.command()
def install(remove: bool = typer.Option(False, "--remove", help="stop and remove the login service")):
    """Make the engine a login service so closing the window can't stop the season.

    Writes ~/Library/LaunchAgents/com.mishpacha.daemon.plist and loads it.
    launchd starts the engine at login and restarts it if it ever dies.
    `Mishpacha.command` then opens as a viewer of the running engine."""
    from . import runner as rn
    if remove:
        rn.uninstall()
        con.print("[yellow]login service removed.[/] Nothing runs unless a Mishpacha window is open.")
        return
    outcome = rn.install()
    if outcome.startswith("failed"):
        con.print(f"[red]{outcome}[/]")
        raise typer.Exit(1)
    for _ in range(20):
        if rn.engine_alive():
            break
        time.sleep(1)
    if rn.engine_alive():
        con.print(f"[green]login service {outcome}[/] — engine is running (pid in data/runner_status.json). "
                  "Open Mishpacha.command any time to watch it; closing the window is now harmless.")
    elif rn.engine_loaded():
        con.print(f"[yellow]login service {outcome} and loaded, but the engine hasn't reported yet.[/] "
                  "If a Mishpacha window is running the schedule itself, quit it (q) and the engine takes over. "
                  "Otherwise check logs/daemon.err.")
    else:
        con.print("[red]launchd did not load the service.[/] See logs/daemon.err.")
        raise typer.Exit(1)


@app.command()
def login():
    """One-time: open a browser, sign in to Sleeper, and save the profile.

    Nothing reads or stores your credentials -- the browser profile keeps its
    own session the way it would on any laptop. Scheduled jobs reuse it.
    """
    from .season.executor import login_once
    login_once()


def report_lineup(rep: dict) -> None:
    """Print what set_lineup did. Kept out of the command so it can be tested.

    The report's `swaps` and `unresolved` are (out, in) PAIRS, but `before`
    and `after` are flat lists of NAMES from the cache-busted API read.
    Unpacking `after` as pairs crashed this printout on 2026-09-17 and
    09-20 -- after both swaps had already landed -- so a working move exited
    non-zero and notified "Lineup FAILED" two weeks running.
    """
    if not rep["swaps"]:
        con.print("\n[green]Sleeper already matches -- nothing to do.[/]")
        return
    con.print("\n" + ("[yellow]DRY RUN[/] -- would swap:" if rep["dry_run"] else "[bold]swapped:[/]"))
    for out_, in_ in rep["swaps"]:
        con.print(f"   {out_}  ->  {in_}")
    if rep["unresolved"]:
        con.print("\n[red]did not take (locked or UI changed):[/]")
        for out_, in_ in rep["unresolved"]:
            con.print(f"   {out_}  ->  {in_}")
    if not rep["dry_run"]:
        con.print("\nSleeper now shows: " + ", ".join(rep["after"]))
        if rep.get("verified") is False:
            con.print("[yellow]warning: Sleeper did not confirm the change -- check the roster page.[/]")


@app.command("execute-lineup")
def execute_lineup(
    week: int = typer.Option(None),
    live: bool = typer.Option(False, "--live", help="actually make the swaps (default is a dry run)"),
    headless: bool = typer.Option(True, help="run the browser without a window"),
):
    """Set Sleeper's starters to the engine's optimal lineup. Dry run unless --live."""
    from .data import projections as pj
    from .season.executor import set_lineup
    from .season.lineup import Candidate, optimize_points

    wk = week or sleeper.active_week()
    rosters = sleeper.rosters(cfg.LEAGUE_ID, fresh=True)
    mine = next((r for r in rosters if r.get("owner_id") == cfg.MY_USER_ID), None)
    if not mine or not mine.get("players"):
        con.print("[yellow]no roster on Sleeper yet[/]")
        raise typer.Exit()

    weekly = pj.weekly_projections(week=wk)
    b, _ = get_board()
    ent = {e.player_id: e for e in b}
    cands = [
        Candidate(pid, ent[pid].name, ent[pid].position,
                  weekly[pid].points if pid in weekly else 0.0,
                  ent[pid].injury_status, is_bye=(ent[pid].bye == wk))
        for pid in mine["players"] if pid in ent
    ]
    lu, total = optimize_points(cands)
    desired = {c.name for g in lu.values() for c in g}
    con.print(f"[bold]week {wk}[/] target lineup, {total:.1f} projected:")
    for slot_name, group in lu.items():
        con.print(f"   {slot_name:<5} " + ", ".join(c.name for c in group))

    # Slot-legal pairing needs positions; locked players (game underway) must
    # never be touched -- Sleeper's matchup feed shows who has scored.
    positions = {e.name: e.position for e in b}
    matchup = next((m for m in sleeper.matchups(cfg.LEAGUE_ID, wk)
                    if m.get("roster_id") == mine.get("roster_id")), {})
    locked = {ent[pid].name
              for pid, pts in zip(mine.get("starters") or [], matchup.get("starters_points") or [])
              if pts and pid in ent}
    if locked:
        con.print(f"   [dim]locked (already played): {', '.join(sorted(locked))}[/]")

    rep = set_lineup(desired, dry_run=not live, headless=headless,
                     positions=positions, locked=locked)
    report_lineup(rep)


if __name__ == "__main__":
    app()
