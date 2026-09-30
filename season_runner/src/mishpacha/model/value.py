"""Valuation: turn projected points into draft value for THIS league.

The core idea is value over replacement (VOR): a player is worth the points
they produce above the freely-available alternative at their position. Raw
projected points are misleading -- a QB who scores 380 is not better than a RB
who scores 280, because QB replacement level is ~300 while RB replacement is
~130.

What makes this league unusual is TWO flex slots. Rather than assume a flex
split, `solve_replacement` derives it endogenously: fill base starters, then
let the best remaining RB/WR/TE compete for the 24 league-wide flex slots.
That is literally how a flex works, and it self-corrects if 2026 happens to be
a year where, say, TEs are unusually strong.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..config import (
    FLEX_ELIGIBLE,
    FLEX_SLOTS,
    NUM_TEAMS,
    STARTER_SLOTS,
)


@dataclass(slots=True)
class Projection:
    """A player's season-long projection, already scored in league points."""
    player_id: str
    name: str
    position: str
    team: str | None
    points: float                 # projected season fantasy points (mean)
    games: float = 17.0
    sd: float = 0.0               # season-long standard deviation
    weekly_sd: float = 0.0        # week-to-week volatility (ceiling/floor)
    adp: float | None = None      # consensus average draft position
    adp_sd: float | None = None   # dispersion of that ADP
    injury_status: str | None = None
    bye_week: int | None = None
    sources: dict = field(default_factory=dict)

    @property
    def ppg(self) -> float:
        return self.points / self.games if self.games else 0.0


@dataclass(slots=True)
class Valuation:
    projection: Projection
    replacement_points: float
    vor: float                    # value over replacement, season points
    vor_rank: int = 0
    pos_rank: int = 0
    tier: int = 0
    risk_discount: float = 0.0    # points shaved for injury/situation risk
    adjusted_vor: float = 0.0

    @property
    def player_id(self) -> str:
        return self.projection.player_id

    @property
    def name(self) -> str:
        return self.projection.name

    @property
    def position(self) -> str:
        return self.projection.position


# How much of a position's projected spread should be believed, fitted by
# regressing 2025 ACTUAL ppg on RAW 2025 PROJECTED ppg (scripts/calibrate.py).
#
# This started as a guess that kicker and defense projections were unreliable
# enough to shrink 70% toward the positional mean. The measurement says the
# guess was wrong in DIRECTION: the fitted slopes are 2.32 (K) and 1.95 (DEF),
# meaning realized spread is WIDER than projected, with correlations of 0.49
# and 0.40 -- moderate, but real signal. Skill positions come out at 0.88-1.16,
# i.e. take the projection as given.
#
# So nothing is shrunk. The two problems shrinkage was covering for are handled
# structurally instead, which is where they belonged:
#   * blindness to a position -> MAX_REPLACEMENT_SHARE caps the baseline below
#     the best player, so someone is always worth rostering.
#   * drafting a defense in round 8 -> DraftBrain.candidates defers K/DEF to
#     the closing rounds outright.
#
# (Caveat: n is only 19-20 for K/DEF, so these slopes are noisy. The safe
# reading is "no evidence for shrinking", not "the spread is precisely 2x".)
RELIABILITY: dict[str, float] = {}


def shrink_unreliable(
    projections: list[Projection], reliability: dict[str, float] | None = None
) -> list[Projection]:
    """Regress low-signal positions toward their positional mean, in place."""
    rel = reliability or RELIABILITY
    by_pos: dict[str, list[Projection]] = {}
    for p in projections:
        if p.position in rel:
            by_pos.setdefault(p.position, []).append(p)

    for pos, group in by_pos.items():
        k = rel[pos]
        # Mean over the players actually rosterable at the position, not the
        # long tail of third-string kickers, which would drag the mean down.
        head = sorted(group, key=lambda x: -x.points)[: NUM_TEAMS * 2]
        mean = sum(x.points for x in head) / max(len(head), 1)
        for x in group:
            x.points = round(mean + k * (x.points - mean), 2)
    return projections


def solve_replacement(
    projections: list[Projection],
    num_teams: int = NUM_TEAMS,
    flex_slots: int = FLEX_SLOTS,
    starter_slots: dict[str, int] | None = None,
) -> tuple[dict[str, float], dict[str, int]]:
    """Derive replacement-level points per position, solving flex endogenously.

    Returns (replacement_points_by_pos, starter_count_by_pos).

    Method:
      1. Fill each position's base starters league-wide (e.g. 12 QB, 24 RB).
      2. Pool everyone left at flex-eligible positions, sort by points, and
         take the top (num_teams * flex_slots) as the flex starters.
      3. Replacement level = the best player who does NOT start anywhere.
    """
    slots = starter_slots or STARTER_SLOTS
    by_pos: dict[str, list[Projection]] = {}
    for p in projections:
        by_pos.setdefault(p.position, []).append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda x: x.points, reverse=True)

    starters: dict[str, int] = {}
    leftovers: list[Projection] = []

    for pos, per_team in slots.items():
        need = per_team * num_teams
        pool = by_pos.get(pos, [])
        starters[pos] = min(need, len(pool))
        if pos in FLEX_ELIGIBLE:
            leftovers.extend(pool[need:])

    # Flex competition across RB/WR/TE.
    leftovers.sort(key=lambda x: x.points, reverse=True)
    for p in leftovers[: num_teams * flex_slots]:
        starters[p.position] = starters.get(p.position, 0) + 1

    replacement: dict[str, float] = {}
    for pos, pool in by_pos.items():
        n = starters.get(pos, 0)
        if not pool:
            replacement[pos] = 0.0
        elif n < len(pool):
            replacement[pos] = pool[n].points      # best non-starter
        else:
            replacement[pos] = pool[-1].points
    return replacement, starters


def risk_discount(p: Projection) -> float:
    """Fraction of projected points to shave for availability risk.

    Deliberately conservative: preseason injury tags are noisy, and over-fading
    a tagged star is a bigger mistake than under-fading one. Age curves are
    applied only where the historical cliff is steep (RB).
    """
    d = 0.0
    status = (p.injury_status or "").strip()
    if status in ("IR", "PUP", "NA", "DNR", "Sus"):
        d += 0.35
    elif status == "Out":
        d += 0.12
    elif status == "Doubtful":
        d += 0.08
    elif status == "Questionable":
        d += 0.03          # preseason "Questionable" is mostly noise

    # RB age cliff is real and steep; WR/TE much flatter.
    # (no age data -> no discount)
    return min(d, 0.5)


def find_tiers(
    vals: list[Valuation],
    gap_multiplier: float = 2.0,
    abs_floor: float = 8.0,
) -> None:
    """Assign tiers within each position by robust gap detection.

    A tier break is declared where the points drop between consecutive players
    is BOTH (a) more than `gap_multiplier` times the median gap for that
    position, and (b) at least `abs_floor` season points in absolute terms.

    The median is used rather than the mean because a couple of huge cliffs at
    the top would otherwise inflate the threshold and hide every later break.
    Requiring both conditions means a smoothly-decaying position correctly
    collapses to a single tier instead of one tier per player.

    Tiers matter more than ranks in a draft: taking the last player of a tier
    is nearly free, while reaching into the next tier early is a real cost.
    """
    by_pos: dict[str, list[Valuation]] = {}
    for v in vals:
        by_pos.setdefault(v.position, []).append(v)

    for _pos, group in by_pos.items():
        group.sort(key=lambda v: v.projection.points, reverse=True)
        pts = np.array([v.projection.points for v in group], dtype=float)

        if len(pts) < 3:
            for i, v in enumerate(group):
                v.tier, v.pos_rank = 1, i + 1
            continue

        gaps = -np.diff(pts)                       # positive = drop to next
        positive = gaps[gaps > 0]
        median_gap = float(np.median(positive)) if positive.size else 0.0
        thresh = max(median_gap * gap_multiplier, abs_floor)

        tier = 1
        for i, v in enumerate(group):
            if i > 0 and gaps[i - 1] > thresh:
                tier += 1
            v.tier = tier
            v.pos_rank = i + 1


def valuate(
    projections: list[Projection],
    num_teams: int = NUM_TEAMS,
    flex_slots: int = FLEX_SLOTS,
    apply_risk: bool = True,
) -> list[Valuation]:
    """Full valuation pass: replacement levels -> VOR -> risk -> tiers -> ranks."""
    shrink_unreliable(projections)
    replacement, _starters = solve_replacement(projections, num_teams, flex_slots)

    vals: list[Valuation] = []
    for p in projections:
        rep = replacement.get(p.position, 0.0)
        vor = p.points - rep
        disc = risk_discount(p) * p.points if apply_risk else 0.0
        vals.append(
            Valuation(
                projection=p,
                replacement_points=rep,
                vor=round(vor, 2),
                risk_discount=round(disc, 2),
                adjusted_vor=round(vor - disc, 2),
            )
        )

    find_tiers(vals)
    vals.sort(key=lambda v: v.adjusted_vor, reverse=True)
    for i, v in enumerate(vals, 1):
        v.vor_rank = i
    return vals


def positional_scarcity(vals: list[Valuation], horizon: int = 24) -> dict[str, float]:
    """How fast value decays at each position over the next `horizon` picks.

    Steeper decay = more urgent to draft that position now. This is the number
    that should drive positional priority, not gut feel about "RB is scarce".
    """
    by_pos: dict[str, list[Valuation]] = {}
    for v in vals:
        by_pos.setdefault(v.position, []).append(v)

    out: dict[str, float] = {}
    for pos, group in by_pos.items():
        group.sort(key=lambda v: v.adjusted_vor, reverse=True)
        top = [v.adjusted_vor for v in group[:horizon]]
        if len(top) < 2:
            out[pos] = 0.0
            continue
        out[pos] = round((top[0] - top[-1]) / max(len(top) - 1, 1), 3)
    return out
