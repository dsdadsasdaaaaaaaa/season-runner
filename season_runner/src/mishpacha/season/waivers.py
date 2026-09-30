"""Waiver targets and FAAB bid sizing.

FAAB is a blind auction with a fixed $100 season budget, so every bid is a bet
against eleven other bidders you cannot see. Two failure modes dominate:
hoarding (finishing the year with $60 unspent while a league-winner went for
$23) and splurging (dropping $70 in week 2 on a hot streak that regresses).

The sizing model here is grounded in what a player is actually worth to US:
the marginal points he adds to our starting lineup over the rest of the season,
divided by the points our whole remaining budget could theoretically buy. It is
then adjusted for how contested the player is, since a blind auction requires
paying a premium precisely when everyone else wants the same guy.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import FAAB_BUDGET, REGULAR_SEASON_WEEKS


@dataclass(slots=True)
class WaiverTarget:
    player_id: str
    name: str
    position: str
    team: str | None
    proj_ppg: float
    marginal_ppg: float          # points added to OUR starting lineup
    weeks_left: int
    trending_adds: int = 0
    bid_pct: float = 0.0
    bid_dollars: int = 0
    rationale: str = ""
    drop_id: str | None = None   # who makes room; marginal_ppg is net of him
    drop_name: str | None = None

    @property
    def season_gain(self) -> float:
        return self.marginal_ppg * self.weeks_left


def contested_multiplier(trending_adds: int) -> float:
    """Blind auctions demand a premium on players everyone wants.

    Sleeper's trending-adds count is a live proxy for how many managers are
    about to bid on the same person. Being outbid by $1 is the worst outcome in
    FAAB -- you pay nothing and get nothing -- so contested players warrant
    paying above their standalone value.
    """
    if trending_adds >= 100_000: return 1.45
    if trending_adds >= 40_000:  return 1.30
    if trending_adds >= 10_000:  return 1.18
    if trending_adds >= 2_000:   return 1.08
    return 1.0


def urgency(weeks_left: int, full_season: int = 13, exponent: float = 0.30,
            cap: float = 1.9) -> float:
    """Spend-down pressure as the season runs out.

    Leftover FAAB is worth exactly nothing in January, so the same weekly
    upgrade is worth bidding MORE for in week 12 than in week 2 -- there are
    fewer remaining chances to deploy the budget, and no salvage value for
    hoarding it. Dying with $40 unspent while a rival wins the title on a $23
    claim is the single most common FAAB error.

    (Note this deliberately runs opposite to raw season-points math, which
    would say a late add is worth less. Both effects are real; the budget's
    zero terminal value is the larger one.)
    """
    return min(cap, (full_season / max(weeks_left, 1)) ** exponent)


def size_bid(
    marginal_ppg: float,
    weeks_left: int,
    budget_left: int,
    trending_adds: int = 0,
    max_share: float = 0.6,
) -> tuple[int, float, str]:
    """Return (dollars, share_of_remaining_budget, rationale)."""
    weeks_left = max(weeks_left, 1)
    gain = max(marginal_ppg, 0.0) * weeks_left

    # Calibration anchor: a player adding ~6 ppg for the rest of the season is
    # a genuine league-winner (think the backup who just inherited a bell-cow
    # role) and justifies roughly half the budget.
    #
    # The mapping is CONVEX, not linear. A linear curve badly overpays for
    # marginal adds -- it priced a 2 ppg flex piece at 27% of budget, which is
    # how managers end up broke in October. The 1.5 exponent reproduces
    # standard practice: ~50% for a league-winner, ~27% for a clear starter,
    # ~10% for a flex piece, low single digits for a stash.
    anchor = 6.0 * weeks_left
    ratio = (gain / anchor) if anchor else 0.0
    share = 0.5 * (ratio ** 1.5)
    share *= contested_multiplier(trending_adds)
    share *= urgency(weeks_left)
    share = min(share, max_share)

    dollars = int(round(share * max(budget_left, 0)))
    # A $0 bid still wins uncontested claims by waiver priority; never bid
    # negative, and never bid the entire budget on a non-elite add.
    dollars = max(0, min(dollars, budget_left))

    if gain <= 0:
        why = "adds nothing to our starting lineup -- claim only at $0"
    elif share >= 0.30:
        why = f"league-winning add: +{marginal_ppg:.1f} ppg for {weeks_left} weeks"
    elif share >= 0.12:
        why = f"immediate starter upgrade: +{marginal_ppg:.1f} ppg"
    elif share >= 0.04:
        why = f"flex/depth value: +{marginal_ppg:.1f} ppg"
    else:
        why = "speculative stash -- bid small or not at all"
    if trending_adds >= 10_000:
        why += f" (heavily contested: {trending_adds:,} adds league-wide)"
    return dollars, round(share, 3), why


def weeks_remaining(current_week: int) -> int:
    return max(REGULAR_SEASON_WEEKS - current_week + 1, 1)


def rank_targets(
    targets: list[WaiverTarget],
    budget_left: int = FAAB_BUDGET,
) -> list[WaiverTarget]:
    """Size every bid and order by value added."""
    for t in targets:
        d, s, why = size_bid(t.marginal_ppg, t.weeks_left, budget_left, t.trending_adds)
        t.bid_dollars, t.bid_pct, t.rationale = d, s, why
    targets.sort(key=lambda t: -t.season_gain)
    return targets
