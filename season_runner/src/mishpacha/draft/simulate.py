"""Monte Carlo draft simulation -- the decision engine.

Ranked lists answer "who is the best player?". That is the wrong question. The
right question is "which pick leaves me with the best FINAL ROSTER, given how
the other eleven teams will actually behave between now and my next pick?"

Method, per candidate player:
  1. Draw a realized draft order: every player's ADP is perturbed by its own
     measured standard deviation, so each simulation is a plausible alternate
     universe of how the room drafts.
  2. Force our current pick to be the candidate.
  3. Walk the remaining draft. Opponents take the best available player in that
     universe's order, skipping positions they are already full at. We take the
     pick that maximizes marginal roster value.
  4. Score the final roster by expected starting-lineup points across the
     season, honoring byes and both FLEX slots.

Averaging over simulations prices positional scarcity, tier cliffs, runs, and
roster construction simultaneously, with no hand-tuned positional rules.

Variance reduction: every candidate is evaluated against the SAME set of drawn
universes (common random numbers), so differences between candidates reflect
the candidates rather than luck of the draw.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

import numpy as np

from ..config import (
    DRAFT_ROUNDS,
    FLEX_ELIGIBLE,
    FLEX_SLOTS,
    MAX_ROSTER_SHAPE,
    NUM_TEAMS,
    REGULAR_SEASON_WEEKS,
    STARTER_SLOTS,
    slot_for_pick,
)
from ..model.roster import RosterPlayer, season_value

# Opponents rarely take a 3rd QB / 2nd K etc. Caps keep simulated opponents
# behaving like humans instead of hoovering up one position.
OPPONENT_CAPS = {"QB": 2, "TE": 2, "K": 1, "DEF": 1, "RB": 7, "WR": 8}

# Our own caps are tighter. With 10 starting slots and only 5 bench spots there
# is no room for a second kicker or defense -- every spare slot should be a
# flex-eligible body who can actually enter the lineup.
OUR_CAPS = {"QB": 2, "TE": 2, "K": 1, "DEF": 1, "RB": 7, "WR": 8}

# Baseline probability a player is active in a given non-bye week, by position.
# Reflects real historical availability: running backs miss meaningfully more
# time than receivers, and kickers/defenses essentially never miss. These are
# what give bench depth its value in the objective function.
AVAILABILITY = {"QB": 0.88, "RB": 0.84, "WR": 0.87, "TE": 0.86, "K": 0.97, "DEF": 1.0}

# Multiplier applied on top for a player carrying an injury designation now.
INJURY_MULT = {
    "IR": 0.35, "PUP": 0.45, "NA": 0.5, "DNR": 0.5, "Sus": 0.6,
    "Out": 0.75, "Doubtful": 0.82, "Questionable": 0.95,
}


def availability_for(position: str, injury_status: str | None) -> float:
    base = AVAILABILITY.get(position, 0.88)
    return round(base * INJURY_MULT.get((injury_status or "").strip(), 1.0), 4)

# How many of the best available players we evaluate for each of OUR picks.
OUR_BRANCHING = 10


@dataclass(slots=True)
class SimPlayer:
    idx: int
    player_id: str
    name: str
    position: str
    points: float
    ppg: float
    adp: float
    adp_sd: float
    bye: int | None
    avail: float = 0.88


@dataclass
class SimResult:
    player_id: str
    name: str
    position: str
    adp: float
    mean_value: float
    sd_value: float
    win_rate: float = 0.0          # share of universes where this is the best pick
    p10: float = 0.0
    p90: float = 0.0
    sample_rosters: list = field(default_factory=list)


def to_sim_players(board_entries, limit: int = 320) -> list[SimPlayer]:
    """Take the top slice of the board that is realistically draftable."""
    pool = sorted(board_entries, key=lambda e: e.adp)[:limit]
    out = []
    for i, e in enumerate(pool):
        games = e.valuation.projection.games or 17.0
        out.append(
            SimPlayer(
                idx=i, player_id=e.player_id, name=e.name, position=e.position,
                points=e.points, ppg=e.points / max(games, 1.0),
                adp=e.adp, adp_sd=max(e.adp_sd, 0.5), bye=e.bye,
                avail=availability_for(e.position, e.injury_status),
            )
        )
    return out


def replacement_ppg(
    board_entries, num_teams: int = NUM_TEAMS, rounds: int = DRAFT_ROUNDS
) -> dict[str, float]:
    """Per-game points of the best player genuinely available on WAIVERS.

    This is NOT the same as draft replacement level, and conflating the two is
    a trap. Draft replacement (used for VOR) is the best non-STARTER -- QB13 in
    a 12-team league. But QB13 is rostered by somebody; you cannot stream him.
    Using him as the waiver baseline implies a free-agent QB scores ~17 ppg,
    which makes every real QB look worthless and produces rosters with one
    running back.

    The true waiver baseline is the best player nobody rostered. We derive the
    rostered depth at each position empirically, from the positional mix of the
    top `num_teams * rounds` players by ADP -- i.e. what the room will actually
    draft -- and then take the next man at each position.
    """
    slots = num_teams * rounds
    drafted = sorted(board_entries, key=lambda e: e.adp)[:slots]

    counts: dict[str, int] = {}
    for e in drafted:
        counts[e.position] = counts.get(e.position, 0) + 1

    by_pos: dict[str, list] = {}
    for e in board_entries:
        by_pos.setdefault(e.position, []).append(e)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda e: -e.points)

    out: dict[str, float] = {}
    for pos, pool in by_pos.items():
        idx = min(counts.get(pos, 0), len(pool) - 1)
        e = pool[idx]
        games = e.valuation.projection.games or 17.0
        out[pos] = round(e.points / max(games, 1.0), 3)

    best, ranked = {}, {}
    for pos, pool in by_pos.items():
        vals = [e.points / max(e.valuation.projection.games or 17.0, 1.0) for e in pool]
        best[pos] = vals[0]
        ranked[pos] = vals
    return apply_streaming_premium(out, best, ranked)


# Streaming premium: what you actually get by picking the best matchup each
# week, expressed as a multiple of the static season-long waiver baseline.
#
# The static baseline assumes you are stuck all year with the single best
# unrostered player. That is wrong for positions that are matchup-driven and
# freely available: you drop and re-add a defense every week, chasing whoever
# faces the worst offense. Defenses swing enormously by opponent, kickers less
# so, quarterbacks less again. Without this correction the engine overvalues
# rostering a defense and takes one several rounds too early.
#
# CALIBRATION WARNING, learned the hard way. A premium large enough to lift the
# baseline above the BEST player at that position makes the objective blind to
# it: every candidate then has marginal value exactly 0.00, and the engine picks
# among them by Monte Carlo noise. At 1.35x for DEF and 1.15x for K, replacement
# (6.83 / 6.71) sat above every defense (best 6.24) and every kicker (best 6.44)
# on the board -- selection came out ANTI-correlated with quality, rank
# correlation -0.81, discarding roughly 23 season points. `cap_premium` below
# now enforces the invariant structurally.
# Measured against 2025 (scripts/calibrate.py) with an IMPLEMENTABLE rule --
# each week start the highest-PROJECTED free agent, decided in advance -- not
# the hindsight best, which flatters streaming to 2.5x and is uncapturable.
#
#   DEF  hold-best-static 6.33 ppg -> project-and-stream 7.50 ppg  = 1.18x
#   K    hold-best-static 8.20 ppg -> project-and-stream 8.14 ppg  = 0.99x
#   QB   hold-best-static 13.35 ppg -> project-and-stream 16.68 ppg = 1.25x
#
# Kicker streaming gains literally nothing: weekly kicker projections carry no
# usable signal, so chasing matchups is churn. Quarterback streaming is worth
# far more than assumed, which is another reason not to spend an early pick
# there.
STREAMING_PREMIUM = {"DEF": 1.18, "K": 1.00, "QB": 1.25}

# Two structural guards on the baseline, both learned from failures.
#
# MAX_REPLACEMENT_SHARE: the baseline may never exceed this share of the BEST
# player at a position. Without it a 1.35x defense premium put replacement
# above every defense in existence, marginal value became exactly 0.00 for all
# of them, and selection came out anti-correlated with quality.
#
# MIN_ABOVE_REPLACEMENT: at least this many players must clear the baseline.
# The cap alone is not enough -- a measured 1.25x quarterback premium left
# exactly ONE quarterback above replacement, which is blindness in all but
# name. The deeper cause is a scale mismatch: the premiums are measured on
# REALIZED points, while these baselines live on the PROJECTED scale, which is
# compressed (QB projected sd 1.79 vs actual 2.96). Rather than invent a
# conversion factor, require that a useful fraction of a league's worth of
# players stay worth rostering.
MAX_REPLACEMENT_SHARE = 0.92
MIN_ABOVE_REPLACEMENT = 6


def apply_streaming_premium(
    rep: dict[str, float],
    best: dict[str, float] | None = None,
    ranked: dict[str, list[float]] | None = None,
) -> dict[str, float]:
    """Lift streamable positions, then clamp so the position stays visible.

    Streaming beats a season-long baseline because you pick the matchup each
    week -- but it cannot beat the best player at the position, or nobody is
    worth rostering and the engine goes blind to the whole position.

    `ranked` gives each position's per-game values, best first, so the baseline
    can be pulled down to the MIN_ABOVE_REPLACEMENT-th player when a premium
    would otherwise clear the field.
    """
    out = {k: round(v * STREAMING_PREMIUM.get(k, 1.0), 3) for k, v in rep.items()}
    for pos in list(out):
        ceiling = (best or {}).get(pos)
        if ceiling and ceiling > 0:
            out[pos] = min(out[pos], ceiling * MAX_REPLACEMENT_SHARE)
        vals = (ranked or {}).get(pos) or []
        if len(vals) >= MIN_ABOVE_REPLACEMENT:
            # The Nth best must still CLEAR the baseline. The margin has to
            # exceed the 3-decimal rounding below, or it rounds back to a tie.
            out[pos] = min(out[pos], vals[MIN_ABOVE_REPLACEMENT - 1] - 0.01)
        out[pos] = round(out[pos], 3)
    return out


def rostered_depth(board_entries, num_teams: int = NUM_TEAMS, rounds: int = DRAFT_ROUNDS):
    """Positional mix of the players the room will actually draft."""
    drafted = sorted(board_entries, key=lambda e: e.adp)[: num_teams * rounds]
    counts: dict[str, int] = {}
    for e in drafted:
        counts[e.position] = counts.get(e.position, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def _fast_lineup_points(
    by_pos: dict[str, list[float]], rep: dict[str, float] | None = None
) -> float:
    """Optimal starting-lineup ppg from per-position sorted ppg lists.

    Greedy is optimal for this slot structure (dedicated slots are
    position-locked; FLEX takes the best remainder).
    """
    rep = rep or {}
    total = 0.0
    leftovers: list[float] = []
    for pos, n in STARTER_SLOTS.items():
        vals = by_pos.get(pos) or []
        r = rep.get(pos, 0.0)
        # max(player, stream): a sub-replacement player is benched, not started.
        for i in range(n):
            total += max(vals[i], r) if i < len(vals) else r
        if pos in FLEX_ELIGIBLE:
            leftovers.extend(vals[n:])
    leftovers.sort(reverse=True)
    flex_rep = max((rep.get(q, 0.0) for q in FLEX_ELIGIBLE), default=0.0)
    for i in range(FLEX_SLOTS):
        total += max(leftovers[i], flex_rep) if i < len(leftovers) else flex_rep
    return total


def _add_sorted(by_pos: dict[str, list[float]], pos: str, ppg: float) -> None:
    lst = by_pos.setdefault(pos, [])
    lst.append(ppg)
    lst.sort(reverse=True)


def _marginal(
    by_pos: dict[str, list[float]], pos: str, ppg: float, base: float,
    rep: dict[str, float] | None = None,
) -> float:
    """Lineup-point gain from adding this player. Cheap: add, measure, remove."""
    lst = by_pos.setdefault(pos, [])
    lst.append(ppg)
    lst.sort(reverse=True)
    gain = _fast_lineup_points(by_pos, rep) - base
    lst.remove(ppg)
    return gain


def _roster_counts(by_pos: dict[str, list[float]]) -> dict[str, int]:
    return {k: len(v) for k, v in by_pos.items()}


def empirical_survival(
    players: list[SimPlayer],
    taken: set[str],
    from_pick: int,
    to_pick: int,
    n_sims: int = 600,
    num_teams: int = NUM_TEAMS,
    seed: int = 4242,
    assume_we_take: str | None = None,
) -> dict[str, float]:
    """P(each player survives to `to_pick`), measured from the SAME opponent
    model the recommendation engine uses.

    Replaces a separate analytic hazard model that disagreed with the engine's
    own behaviour. Measured against this simulator the analytic version was
    systematically PESSIMISTIC -- understating availability by 4 points on
    average and up to 14 points in the ADP 15-20 band, which is precisely the
    range that decides whether to reach for someone at our second pick.

    Two models of the same quantity will always drift apart. One does not.
    """
    n = len(players)
    if n == 0 or to_pick <= from_pick:
        return {p.player_id: 1.0 for p in players}

    adp = np.array([p.adp for p in players])
    sd = np.array([p.adp_sd for p in players])
    rng = np.random.default_rng(seed)

    survived = {p.player_id: 0 for p in players}
    for _ in range(n_sims):
        order = np.argsort(adp + rng.standard_normal(n) * sd)
        gone = set(taken)
        if assume_we_take:
            gone.add(assume_we_take)
        caps: dict[int, dict[str, int]] = {}
        cursor = 0
        for pick in range(from_pick + 1, to_pick):
            sl = slot_for_pick(pick, num_teams)
            oc = caps.setdefault(sl, {})
            while cursor < n:
                cand = players[order[cursor]]
                if cand.player_id in gone:
                    cursor += 1
                    continue
                if oc.get(cand.position, 0) >= OPPONENT_CAPS.get(cand.position, 99):
                    nxt = None
                    for j in range(cursor, min(cursor + 40, n)):
                        q = players[order[j]]
                        if q.player_id in gone:
                            continue
                        if oc.get(q.position, 0) < OPPONENT_CAPS.get(q.position, 99):
                            nxt = q
                            break
                    if nxt is None:
                        cursor += 1
                        continue
                    cand = nxt
                gone.add(cand.player_id)
                oc[cand.position] = oc.get(cand.position, 0) + 1
                break
            else:
                break
        for pl in players:
            if pl.player_id not in gone:
                survived[pl.player_id] += 1

    return {k: v / n_sims for k, v in survived.items()}


def simulate_candidates(
    players: list[SimPlayer],
    taken: set[str],
    my_roster_ids: list[str],
    current_pick: int,
    candidates: list[SimPlayer],
    n_sims: int = 400,
    my_slot: int = 6,
    num_teams: int = NUM_TEAMS,
    rounds: int = DRAFT_ROUNDS,
    seed: int = 12345,
    weeks: int = REGULAR_SEASON_WEEKS,
    replacement: dict[str, float] | None = None,
    opponent_counts: dict[int, dict[str, int]] | None = None,
) -> list[SimResult]:
    """Evaluate each candidate by simulated final-roster value."""
    by_id = {p.player_id: p for p in players}
    # Our own roster must never be silently dropped. Players outside the
    # ADP-sliced pool (a deep kicker, a late defense) used to vanish here, so
    # the engine believed those slots were still empty and recommended a
    # SECOND kicker over a startable back -- with full confidence.
    missing = [pid for pid in my_roster_ids if pid not in by_id]
    if missing:
        raise ValueError(
            f"{len(missing)} rostered player(s) absent from the simulation pool: "
            f"{missing[:5]}. Widen pool_limit or include roster ids explicitly."
        )
    my_current = [by_id[pid] for pid in my_roster_ids]

    total_picks = num_teams * rounds
    my_picks = {
        p for p in range(1, total_picks + 1)
        if slot_for_pick(p, num_teams) == my_slot
    }

    adp = np.array([p.adp for p in players])
    sd = np.array([p.adp_sd for p in players])
    n = len(players)

    rng = np.random.default_rng(seed)
    # Common random numbers: one shared set of universes for all candidates.
    noise = rng.standard_normal((n_sims, n))
    orders = np.argsort(adp[None, :] + noise * sd[None, :], axis=1)

    taken_base = set(taken)
    opponent_counts = opponent_counts or {}
    results: list[SimResult] = []
    per_sim_scores: dict[str, np.ndarray] = {}

    for cand in candidates:
        scores = np.empty(n_sims)

        for s in range(n_sims):
            order = orders[s]
            gone = set(taken_base)
            gone.add(cand.player_id)

            # our roster state
            by_pos: dict[str, list[float]] = {}
            mine: list[SimPlayer] = list(my_current)
            for p in my_current:
                _add_sorted(by_pos, p.position, p.ppg)
            _add_sorted(by_pos, cand.position, cand.ppg)
            mine.append(cand)

            # Opponent roster position counts. Seeded from the REAL board:
            # `taken` records who is gone but not who owns them, so resetting
            # to zero made the caps non-binding mid-draft -- at pick 163 the
            # sim believed a team with six running backs had none.
            opp_counts: dict[int, dict[str, int]] = {
                sl: dict(opponent_counts.get(sl, {}))
                for sl in range(1, num_teams + 1) if sl != my_slot
            }

            cursor = 0
            for pick in range(current_pick + 1, total_picks + 1):
                if pick in my_picks:
                    if len(mine) >= rounds:
                        continue
                    base = _fast_lineup_points(by_pos, replacement)
                    counts = _roster_counts(by_pos)
                    best, best_gain, best_key = None, -1e9, (-1e9, -1e9)
                    seen = 0
                    for i in order:
                        if seen >= OUR_BRANCHING:
                            break
                        p = players[i]
                        if p.player_id in gone:
                            continue
                        if counts.get(p.position, 0) >= OUR_CAPS.get(p.position, 99):
                            continue
                        seen += 1
                        g = _marginal(by_pos, p.position, p.ppg, base, replacement)
                        # Tie-break on raw quality. Without this, every pick
                        # after the starting lineup is full has marginal gain
                        # 0 and the engine picks arbitrarily -- which is how it
                        # ended up drafting a second defense over a startable
                        # running back.
                        key = (round(g, 4), p.ppg)
                        if best is None or key > best_key:
                            best, best_gain, best_key = p, g, key
                    if best is None:
                        continue
                    gone.add(best.player_id)
                    _add_sorted(by_pos, best.position, best.ppg)
                    mine.append(best)
                else:
                    sl = slot_for_pick(pick, num_teams)
                    oc = opp_counts.setdefault(sl, {})
                    while cursor < n:
                        p = players[order[cursor]]
                        if p.player_id in gone:
                            cursor += 1
                            continue
                        if oc.get(p.position, 0) >= OPPONENT_CAPS.get(p.position, 99):
                            # skip this player for this team, but do not burn him
                            nxt = None
                            for j in range(cursor, min(cursor + 40, n)):
                                q = players[order[j]]
                                if q.player_id in gone:
                                    continue
                                if oc.get(q.position, 0) < OPPONENT_CAPS.get(q.position, 99):
                                    nxt = q
                                    break
                            if nxt is None:
                                cursor += 1
                                continue
                            p = nxt
                        gone.add(p.player_id)
                        oc[p.position] = oc.get(p.position, 0) + 1
                        break
                    else:
                        continue

            roster = [
                RosterPlayer(p.player_id, p.position, p.ppg, p.bye, p.avail)
                for p in mine
            ]
            # Shared RNG stream per universe keeps common random numbers intact
            # across candidates, so differences reflect the pick, not luck.
            scores[s] = season_value(
                roster, weeks=weeks, rng=random.Random(seed + s), replacement=replacement
            )

        per_sim_scores[cand.player_id] = scores
        results.append(
            SimResult(
                player_id=cand.player_id, name=cand.name, position=cand.position,
                adp=cand.adp,
                mean_value=float(scores.mean()), sd_value=float(scores.std()),
                p10=float(np.percentile(scores, 10)), p90=float(np.percentile(scores, 90)),
            )
        )

    # How often each candidate is the best choice in a given universe.
    #
    # Ties share credit. Plain argmax always resolves a tie to the first row,
    # so when every candidate scored identically -- routine in the last rounds,
    # where the remaining players cannot crack the lineup -- the engine would
    # report 0% confidence in the very pick it was recommending.
    if per_sim_scores:
        mat = np.vstack([per_sim_scores[r.player_id] for r in results])
        best = mat.max(axis=0)
        is_best = mat >= best - 1e-9
        share = is_best / is_best.sum(axis=0, keepdims=True)
        for i, r in enumerate(results):
            r.win_rate = float(share[i].mean())

    results.sort(key=lambda r: r.mean_value, reverse=True)
    return results
