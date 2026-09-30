"""Evaluate how good a finished roster actually is.

This is the objective function the whole draft optimizes. It is NOT the sum of
player values -- it is the points your OPTIMAL STARTING LINEUP scores, summed
across the regular season, with bye weeks honored.

That distinction matters enormously in this league:
  - Two FLEX slots mean a 3rd/4th good RB or WR actually starts, so depth
    converts to points instead of rotting on the bench.
  - Only 5 bench spots mean hoarding is punished -- a 6th RB who never starts
    contributes nothing.
  - Byes bite hard: with 10 of 15 roster spots starting every week, a stacked
    bye week forces you to start replacement-level scrubs.
"""
from __future__ import annotations

import random
import zlib
from dataclasses import dataclass

from ..config import FLEX_ELIGIBLE, FLEX_SLOTS, REGULAR_SEASON_WEEKS, STARTER_SLOTS


_RNG = random.Random(20260823)
DEFAULT_SEED = 20260823


def _availability_draw(seed: int, position: str, rank: int, week: int) -> float:
    """Deterministic uniform draw keyed by (seed, roster SLOT, week).

    The slot is (position, rank-within-position-by-projection) -- deliberately
    not the player's identity or his index in a list. Three bugs die here:

    * List-order dependence. The original consumed one random number per player
      in list order, so removing a mid-list player re-rolled everyone after
      him: swapping a player for an identical clone scored +17.3 season points
      instead of 0, and FAAB bids were sized off a single misaligned draw.
    * Identity-keyed noise. Keying on player_id fixed the ordering but left two
      distinct players drawing independent luck, so a like-for-like swap still
      showed a systematic +5.0 -- enough to flip a marginal trade verdict.
    * Single-draw verdicts. See expected_season_value.

    Keying on the slot gives true common random numbers: two rosters that
    differ by one player experience the SAME season, so the comparison isolates
    the change instead of measuring who got luckier.
    """
    key = f"{seed}|{position}|{rank}|{week}".encode()
    return (zlib.crc32(key) & 0xFFFFFFFF) / 4294967296.0


@dataclass(slots=True)
class RosterPlayer:
    player_id: str
    position: str
    ppg: float
    bye_week: int | None = None
    avail: float = 0.93
    """Probability the player is active in a given non-bye week.

    Derived from projected games played. This is what gives BENCH DEPTH real
    value: with everyone assumed healthy, a backup RB contributes exactly zero
    and the optimizer will happily draft a second defense instead. Modelling
    availability makes depth pay off exactly as much as it really does.
    """


def optimal_lineup(
    players: list[RosterPlayer],
    exclude_bye: int | None = None,
    replacement: dict[str, float] | None = None,
) -> tuple[float, dict[str, list[str]]]:
    """Best legal starting lineup and its total points.

    Greedy is provably optimal for this slot structure: dedicated slots are
    position-locked so they must take the best at that position, and FLEX
    accepts a superset of what is left, so it takes the best remainder.

    `replacement` gives the points a slot yields when you have nobody to fill
    it. This matters more than it sounds: without it an empty slot scores zero
    forever, which implies you would sit out a week rather than stream anyone.
    That single assumption makes backup QBs, kickers and defenses look far more
    valuable than they are -- in a 12-team league there is always a startable
    QB/K/DEF on waivers, while a genuinely startable RB is scarce. Pricing the
    waiver wire is what keeps the draft engine from wasting bench spots on
    insurance it would never actually need.
    """
    avail = [p for p in players if exclude_bye is None or p.bye_week != exclude_bye]
    by_pos: dict[str, list[RosterPlayer]] = {}
    for p in avail:
        by_pos.setdefault(p.position, []).append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda x: x.ppg, reverse=True)

    rep = replacement or {}
    total = 0.0
    lineup: dict[str, list[str]] = {}
    used: set[str] = set()

    for pos, n in STARTER_SLOTS.items():
        picked = by_pos.get(pos, [])[:n]
        lineup[pos] = [p.player_id for p in picked]
        used.update(p.player_id for p in picked)
        # Each slot is worth the BETTER of the rostered player and the waiver
        # stream. Crediting replacement only for EMPTY slots would force a
        # sub-replacement player into the lineup, which made adding a player
        # able to LOWER a roster's value -- adding a 15.5 ppg backup QB to a
        # QB-less roster scored -2.9. You would simply bench him and stream.
        r = rep.get(pos, 0.0)
        for i in range(n):
            total += max(picked[i].ppg, r) if i < len(picked) else r

    flex_pool = sorted(
        (p for p in avail if p.position in FLEX_ELIGIBLE and p.player_id not in used),
        key=lambda x: x.ppg,
        reverse=True,
    )[:FLEX_SLOTS]
    lineup["FLEX"] = [p.player_id for p in flex_pool]
    flex_rep = max((rep.get(q, 0.0) for q in FLEX_ELIGIBLE), default=0.0)
    for i in range(FLEX_SLOTS):
        total += max(flex_pool[i].ppg, flex_rep) if i < len(flex_pool) else flex_rep

    return total, lineup


def season_value(
    players: list[RosterPlayer],
    weeks: int = REGULAR_SEASON_WEEKS,
    honor_byes: bool = True,
    injuries: bool = True,
    rng: "random.Random | None" = None,
    replacement: dict[str, float] | None = None,
    seed: int | None = None,
) -> float:
    """Expected regular-season points from optimal weekly lineups.

    Walks week by week so that BOTH bye collisions and injury absences are
    priced in. Each week every player is independently available with
    probability `avail`; the optimizer then fields the best legal lineup from
    whoever is left. Averaged across the outer draft simulations, this is what
    makes a strong bench worth drafting -- and it is why the engine stops
    taking a second kicker in round 14.
    """
    if not honor_byes and not injuries:
        pts, _ = optimal_lineup(players, replacement=replacement)
        return pts * weeks

    # `rng` is accepted for backward compatibility; its seed selects the
    # deterministic per-player stream rather than being consumed in list order.
    if seed is None:
        seed = rng.randrange(2**31) if rng is not None else DEFAULT_SEED

    if not injuries:
        total = 0.0
        for wk in range(1, weeks + 1):
            pts, _ = optimal_lineup(players, exclude_bye=wk, replacement=replacement)
            total += pts
        return total

    # Rank within position once; ranks are the slot identity for the whole season.
    ranked: list[tuple[RosterPlayer, str, int]] = []
    by_pos: dict[str, list[RosterPlayer]] = {}
    for p in players:
        by_pos.setdefault(p.position, []).append(p)
    for pos, group in by_pos.items():
        group.sort(key=lambda x: (-x.ppg, x.player_id))
        for rank, p in enumerate(group):
            ranked.append((p, pos, rank))

    total = 0.0
    for wk in range(1, weeks + 1):
        active = [
            p for p, pos, rank in ranked
            if p.bye_week != wk
            and (p.avail >= 1.0 or _availability_draw(seed, pos, rank, wk) < p.avail)
        ]
        pts, _ = optimal_lineup(active, replacement=replacement)
        total += pts
    return total


def expected_season_value(
    players: list[RosterPlayer],
    weeks: int = REGULAR_SEASON_WEEKS,
    draws: int = 48,
    replacement: dict[str, float] | None = None,
    base_seed: int = DEFAULT_SEED,
) -> float:
    """Season value averaged over many independent availability universes.

    A single injury draw is far too noisy to decide anything: swapping a player
    for a statistically IDENTICAL clone scored +25 season points on one draw,
    purely because the two men drew different luck. Any verdict computed from
    one realization -- accept this trade, bid $19 on this waiver -- is reading
    noise. Averaging collapses that to roughly zero, which is the truth.

    The draft simulator does not need this: it already averages across hundreds
    of simulated drafts, so a single draw per draft is the cheaper equivalent.
    """
    if not players:
        return 0.0
    total = 0.0
    for d in range(draws):
        total += season_value(players, weeks=weeks, replacement=replacement,
                              seed=base_seed + d * 7919)
    return total / draws


def marginal_value(
    current: list[RosterPlayer],
    candidate: RosterPlayer,
    weeks: int = REGULAR_SEASON_WEEKS,
) -> float:
    """How many season points adding this player to this roster actually adds.

    This is the number that should drive a pick -- not the player's raw value.
    A great TE added to a roster that already has a great TE is worth far less
    than his ranking suggests, because he can only occupy a FLEX slot.
    """
    before = season_value(current, weeks, injuries=False)
    after = season_value(current + [candidate], weeks, injuries=False)
    return after - before


def bye_week_concentration(players: list[RosterPlayer]) -> dict[int, int]:
    """Count of starters-caliber players sharing each bye week."""
    counts: dict[int, int] = {}
    for p in players:
        if p.bye_week:
            counts[p.bye_week] = counts.get(p.bye_week, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
