"""Trade evaluation.

A trade is worth making when it raises the points our STARTING LINEUP scores,
which is not the same as winning on player quality. Trading two startable
receivers for one better receiver usually loses value here: with two FLEX slots
both of ours were starting, and the bodies we lose have to be replaced from a
waiver pool where the best receiver projects 6.7 ppg.

The evaluator also scores the other side, because a trade only happens if they
say yes. Deals that improve both rosters are the ones that actually get
accepted -- and they exist constantly, since two teams rarely have the same
positional surpluses.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..model.roster import RosterPlayer, expected_season_value


@dataclass
class TradeEvaluation:
    our_gain: float
    their_gain: float
    our_before: float
    our_after: float
    verdict: str
    mutual: bool

    @property
    def accept(self) -> bool:
        return self.our_gain > 0


def evaluate(
    our_roster: list[RosterPlayer],
    their_roster: list[RosterPlayer],
    we_send: list[str],
    we_receive: list[str],
    replacement: dict[str, float] | None = None,
    weeks: int = 14,
) -> TradeEvaluation:
    """Score a proposed trade for both sides."""
    send = {p for p in we_send}
    recv = {p for p in we_receive}

    ours_by_id = {p.player_id: p for p in our_roster}
    theirs_by_id = {p.player_id: p for p in their_roster}

    our_after = [p for p in our_roster if p.player_id not in send]
    our_after += [theirs_by_id[i] for i in recv if i in theirs_by_id]

    their_after = [p for p in their_roster if p.player_id not in recv]
    their_after += [ours_by_id[i] for i in send if i in ours_by_id]

    # Averaged over many availability universes, never a single draw -- see
    # expected_season_value for why a one-shot verdict is noise.
    ob = expected_season_value(our_roster,   weeks=weeks, replacement=replacement)
    oa = expected_season_value(our_after,    weeks=weeks, replacement=replacement)
    tb = expected_season_value(their_roster, weeks=weeks, replacement=replacement)
    ta = expected_season_value(their_after,  weeks=weeks, replacement=replacement)

    og, tg = oa - ob, ta - tb
    mutual = og > 0 and tg > 0
    if og <= 0:
        verdict = "reject -- costs us lineup points"
    elif mutual:
        verdict = "propose it -- improves both rosters, so they should accept"
    elif tg < -0.02 * tb:
        verdict = "good for us but clearly bad for them; unlikely to be accepted"
    else:
        verdict = "favourable to us and roughly neutral for them"

    return TradeEvaluation(round(og, 1), round(tg, 1), round(ob, 1), round(oa, 1), verdict, mutual)


def find_mutual_trades(
    our_roster: list[RosterPlayer],
    their_roster: list[RosterPlayer],
    replacement: dict[str, float] | None = None,
    max_each: int = 2,
    top_n: int = 8,
    weeks: int = 14,
) -> list[tuple[list[str], list[str], TradeEvaluation]]:
    """Search 1-for-1 and 2-for-2 swaps for deals that help both sides.

    Every combination is enumerated and filtered on mutual gain; the deals that
    survive are overwhelmingly surplus-for-need, because that is the only shape
    where both rosters improve. Trading a player who is currently STARTING
    tends to net out near zero -- upgrading a weak slot by five points while
    dropping five points of flex is not a trade, it is a rotation.
    """
    from itertools import combinations

    out: list[tuple[list[str], list[str], TradeEvaluation]] = []
    ours = [p.player_id for p in our_roster]
    theirs = [p.player_id for p in their_roster]

    for k in range(1, max_each + 1):
        for send in combinations(ours, k):
            for recv in combinations(theirs, k):
                ev = evaluate(our_roster, their_roster, list(send), list(recv),
                              replacement, weeks=weeks)
                if ev.mutual:
                    out.append((list(send), list(recv), ev))
    out.sort(key=lambda t: -(t[2].our_gain + t[2].their_gain))
    return out[:top_n]
