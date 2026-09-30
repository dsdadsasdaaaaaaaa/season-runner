"""Weekly lineup optimization.

Two different objectives, and choosing between them is worth real wins:

  * MAXIMIZE POINTS -- start the highest projected total. Correct when you are
    evenly matched.
  * MAXIMIZE WIN PROBABILITY -- when you are a heavy underdog you should chase
    variance (a boom/bust player gives you more paths to an upset than a steady
    one), and when you are a heavy favorite you should suppress it. Starting
    the higher-floor player while projected to lose by 20 is how you lose by 12.

Head-to-head fantasy pays only for wins, so the second objective is the right
one whenever the matchup is lopsided.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..config import FLEX_ELIGIBLE, FLEX_SLOTS, STARTER_SLOTS

# Week-to-week standard deviation as a fraction of a player's projection.
# Receivers are the most volatile (touchdown-dependent, boom/bust), kickers and
# defenses less so in absolute terms but very high relative to their means.
VOLATILITY = {"QB": 0.32, "RB": 0.42, "WR": 0.50, "TE": 0.52, "K": 0.45, "DEF": 0.65}


@dataclass(slots=True)
class Candidate:
    player_id: str
    name: str
    position: str
    proj: float
    injury_status: str | None = None
    opponent: str | None = None
    is_bye: bool = False

    @property
    def sd(self) -> float:
        return self.proj * VOLATILITY.get(self.position, 0.45)

    @property
    def startable(self) -> bool:
        if self.is_bye:
            return False
        return (self.injury_status or "") not in ("Out", "IR", "PUP", "Sus", "DNR", "NA")


def _fill(cands: list[Candidate]) -> dict[str, list[Candidate]]:
    pool = [c for c in cands if c.startable]
    by_pos: dict[str, list[Candidate]] = {}
    for c in pool:
        by_pos.setdefault(c.position, []).append(c)
    for k in by_pos:
        by_pos[k].sort(key=lambda x: -x.proj)

    lineup: dict[str, list[Candidate]] = {}
    used: set[str] = set()
    for pos, n in STARTER_SLOTS.items():
        picked = by_pos.get(pos, [])[:n]
        lineup[pos] = picked
        used |= {p.player_id for p in picked}
    flex = [c for c in pool if c.position in FLEX_ELIGIBLE and c.player_id not in used]
    flex.sort(key=lambda x: -x.proj)
    lineup["FLEX"] = flex[:FLEX_SLOTS]
    return lineup


def optimize_points(cands: list[Candidate]) -> tuple[dict[str, list[Candidate]], float]:
    lineup = _fill(cands)
    total = sum(c.proj for group in lineup.values() for c in group)
    return lineup, total


def win_probability(my_total: float, my_sd: float, opp_total: float, opp_sd: float) -> float:
    """P(we outscore them), treating both totals as normal."""
    sd = math.sqrt(my_sd ** 2 + opp_sd ** 2)
    if sd <= 0:
        return 1.0 if my_total > opp_total else 0.0
    return 0.5 * (1.0 + math.erf((my_total - opp_total) / (sd * math.sqrt(2.0))))


def lineup_stats(lineup: dict[str, list[Candidate]]) -> tuple[float, float]:
    starters = [c for g in lineup.values() for c in g]
    total = sum(c.proj for c in starters)
    sd = math.sqrt(sum(c.sd ** 2 for c in starters))
    return total, sd


def optimize_win_probability(
    cands: list[Candidate],
    opp_total: float,
    opp_sd: float,
    swaps: int = 3,
) -> tuple[dict[str, list[Candidate]], float, float]:
    """Greedy search over single-player swaps to maximize win probability.

    Starts from the points-maximizing lineup and tries swapping any starter for
    any eligible bench player, keeping a swap only if win probability improves.
    Against a strong opponent this systematically promotes high-variance
    players; against a weak one it does the opposite.
    """
    lineup = _fill(cands)
    total, sd = lineup_stats(lineup)
    best_wp = win_probability(total, sd, opp_total, opp_sd)

    bench = [c for c in cands if c.startable and
             c.player_id not in {p.player_id for g in lineup.values() for p in g}]

    for _ in range(swaps):
        improved = False
        for slot, group in list(lineup.items()):
            eligible = FLEX_ELIGIBLE if slot == "FLEX" else {slot}
            for i, starter in enumerate(list(group)):
                for cand in bench:
                    if cand.position not in eligible:
                        continue
                    group[i] = cand
                    t, s = lineup_stats(lineup)
                    wp = win_probability(t, s, opp_total, opp_sd)
                    if wp > best_wp + 1e-6:
                        best_wp = wp
                        bench.remove(cand)
                        bench.append(starter)
                        starter = cand
                        improved = True
                    else:
                        group[i] = starter
        if not improved:
            break

    total, sd = lineup_stats(lineup)
    return lineup, total, best_wp
