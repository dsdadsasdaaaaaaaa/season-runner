"""Out-of-sample backtest: would this engine actually have beaten the room?

A draft engine that cannot be checked is just a confident opinion. This runs
the real thing against a completed season and scores it on what actually
happened.

Setup (all inputs strictly pre-season, no hindsight):
  * 2025 preseason ADP from 8,470 real 12-team PPR drafts run Aug 25-Sep 1 2025.
  * 2025 preseason projections from Sleeper. Verified uncontaminated: they
    correlate 0.747 with final results and badly miss the season's injury
    cases (Jayden Daniels -237, Kyler Murray -233, Nabers -222), which is
    exactly what a genuine forecast looks like.
  * Scored on 2025 ACTUAL production under this league's exact rules.

Three drafters, identical opponents and identical ADP draws:
  * ADP        -- always take the best player left by ADP. This is what
                  Sleeper's autopick does and what most casual managers do.
  * PROJECTION -- take the highest projected points, ignoring scarcity.
  * ENGINE     -- the full Monte Carlo system.

Comparing ENGINE to ADP measures total edge; comparing PROJECTION to ADP
separates "having projections" from "knowing what to do with them".
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import NUM_TEAMS, REGULAR_SEASON_WEEKS, slot_for_pick
from .data.http import get_json
from .draft.simulate import OPPONENT_CAPS, OUR_CAPS, availability_for
from .model.roster import RosterPlayer, optimal_lineup
from .model.scoring import score_line

STATS_URL = "https://api.sleeper.com/stats/nfl/{season}"
_NON_STAT = {"gp", "gms_active", "gs", "off_snp", "tm_def_snp", "tm_off_snp", "tm_st_snp"}


def actual_points(season: str = "2025", ttl: float = 86_400) -> dict[str, tuple[float, float]]:
    """player_id -> (actual season points under our scoring, games played)."""
    params = {"season_type": "regular", "order_by": "pts_ppr"}
    params["position[]"] = ["QB", "RB", "WR", "TE", "K", "DEF"]
    recs = get_json(STATS_URL.format(season=season), params=params, ttl=ttl, timeout=120.0) or []
    out: dict[str, tuple[float, float]] = {}
    for r in recs:
        pid = str(r.get("player_id") or "")
        st = r.get("stats") or {}
        if not pid:
            continue
        pos = ((r.get("player") or {}).get("fantasy_positions") or [None])[0]
        gp = float(st.get("gp") or 0)
        if pos in ("K", "DEF"):
            pts = float(st.get("pts_ppr") or 0.0)      # components incomplete for these
        else:
            clean = {k: v for k, v in st.items()
                     if not k.startswith(("adp", "pts_", "bonus_", "rank")) and k not in _NON_STAT}
            pts = score_line(clean)
        out[pid] = (pts, gp)
    return out


@dataclass
class TeamResult:
    strategy: str
    roster: list[str]
    actual_ppg: float
    actual_season: float


def _score_roster(pids, entries, actuals, basis: str = "per_game") -> float:
    """Optimal lineup value using what players ACTUALLY produced.

    Two bases, because they answer different questions and disagree:

      per_game   -- actual points / games played. Measures how good the players
                    were WHEN AVAILABLE, but under-penalises a star who missed
                    ten weeks; his three big games still set his rate.
      per_week   -- actual points / 17. Charges every missed game to the
                    player, which is what a fantasy manager actually
                    experiences: an empty roster spot on Sunday.

    per_week is the honest measure of a drafted roster. per_game is reported
    alongside it to show the result does not depend on which one you pick.
    """
    rp = []
    for pid in pids:
        e = entries.get(pid)
        if not e:
            continue
        pts, gp = actuals.get(pid, (0.0, 0.0))
        if basis == "per_week":
            rate = pts / 17.0
        else:
            rate = pts / gp if gp else 0.0
        rp.append(RosterPlayer(pid, e.position, rate, e.bye,
                               availability_for(e.position, None)))
    total, _ = optimal_lineup(rp)
    return total


def run_backtest(
    board,
    actuals: dict[str, tuple[float, float]],
    brain,
    my_slot: int = 6,
    rounds: int | None = None,
    trials: int = 40,
    seed: int = 99,
    n_sims: int = 250,
) -> dict[str, list[float]]:
    """Draft the same universe three ways, `trials` times, score on reality.

    `rounds` follows the league config unless overridden. It used to be pinned
    to 15, which quietly desynchronised the harness from the engine when the
    league moved to 16 rounds: the engine deferred kickers and defenses waiting
    for a final round the harness never ran, so it finished with rosters that
    could not field a legal lineup -- and duly "lost" the backtest by 10 ppg.
    """
    from .config import DRAFT_ROUNDS
    from .draft.state import DraftState

    rounds = DRAFT_ROUNDS if rounds is None else rounds

    entries = {e.player_id: e for e in board}
    players = brain.players
    adp = np.array([p.adp for p in players])
    sd = np.array([p.adp_sd for p in players])
    rng = np.random.default_rng(seed)
    total_picks = NUM_TEAMS * rounds
    results: dict[str, list[float]] = {
        f"{k}|{b}": [] for k in ("ADP", "PROJECTION", "ENGINE")
        for b in ("per_game", "per_week")
    }

    proj_rank = {p.player_id: -entries[p.player_id].points for p in players}

    for t in range(trials):
        order = list(np.argsort(adp + rng.standard_normal(len(players)) * sd))

        for strat in ("ADP", "PROJECTION", "ENGINE"):
            st = DraftState(my_slot=my_slot)
            picks: list[dict] = []
            caps: dict[int, dict[str, int]] = {}
            mine: list[str] = []

            for pick in range(1, total_picks + 1):
                slot = slot_for_pick(pick, NUM_TEAMS)
                pid = None

                if slot == my_slot:
                    counts: dict[str, int] = {}
                    for q in mine:
                        counts[entries[q].position] = counts.get(entries[q].position, 0) + 1
                    left = rounds - len(mine)
                    must = [p for p in ("K", "DEF") if counts.get(p, 0) < 1]

                    def ok(pl) -> bool:
                        if must and left <= len(must):
                            return pl.position in must
                        return counts.get(pl.position, 0) < OUR_CAPS.get(pl.position, 99)

                    if strat == "ENGINE":
                        rec = brain.recommend(st, n_sims=n_sims)
                        pid = rec.primary.player_id if rec.primary else None
                    elif strat == "PROJECTION":
                        pool = [players[i] for i in order if players[i].player_id not in st.drafted]
                        pool = [p for p in pool if ok(p)]
                        pool.sort(key=lambda p: proj_rank[p.player_id])
                        pid = pool[0].player_id if pool else None
                    else:  # ADP
                        for i in order:
                            p = players[i]
                            if p.player_id in st.drafted or not ok(p):
                                continue
                            pid = p.player_id
                            break
                    if pid:
                        mine.append(pid)
                else:
                    oc = caps.setdefault(slot, {})
                    for i in order:
                        p = players[i]
                        if p.player_id in st.drafted:
                            continue
                        if oc.get(p.position, 0) >= OPPONENT_CAPS.get(p.position, 99):
                            continue
                        pid = p.player_id
                        oc[p.position] = oc.get(p.position, 0) + 1
                        break

                if not pid:
                    break
                picks.append({"pick_no": pick, "player_id": pid, "draft_slot": slot,
                              "metadata": {"position": entries[pid].position}})
                st.apply_picks(picks, {k: v.position for k, v in entries.items()})

            for basis in ("per_game", "per_week"):
                results[f"{strat}|{basis}"].append(
                    _score_roster(mine, entries, actuals, basis)
                )

    return results
