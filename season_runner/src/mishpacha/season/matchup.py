"""Live matchup projection: expected final score and chance to win this week.

Every starter falls into one of three states, and they must be treated
differently or the number is wrong in exactly the moments you look at it:

  * FINAL        -- his points are banked. No uncertainty left.
  * IN PROGRESS  -- banked points plus his projection scaled by the share of
                    the game still to play. Uncertainty shrinks as the clock
                    runs (variance scales with time left, so sd with its root).
  * NOT STARTED  -- his full projection, full uncertainty.

Adding up both sides gives expected totals and a standard deviation for each;
the win probability is P(our total > theirs) under a normal approximation,
the same model the lineup optimizer uses. Early in the week it is mostly
projection; by Monday night it converges on the real result.

Game states come from ESPN's scoreboard (verified: all 16 games for a week,
with pre/in/post, quarter and clock). Projections come from Sleeper's weekly
feed, scored under this league's rules.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..data.adp import TEAM_ALIASES
from ..data.http import get_json
from .lineup import VOLATILITY

ESPN_WEEK = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
QUARTER_MIN = 15.0


@dataclass
class GameState:
    state: str              # "pre" | "in" | "post"
    remaining: float        # share of the game still to play, 0..1
    detail: str = ""


@dataclass
class PlayerLine:
    slot: str
    name: str
    position: str
    team: str | None
    actual: float
    proj: float
    state: str              # "post" | "in" | "pre" | "bye"
    remaining: float
    detail: str = ""

    @property
    def expected(self) -> float:
        if self.state in ("post", "bye"):
            return self.actual
        return self.actual + self.proj * self.remaining

    @property
    def sd(self) -> float:
        if self.state in ("post", "bye") or self.remaining <= 0:
            return 0.0
        return max(self.proj, 0.0) * VOLATILITY.get(self.position, 0.45) * math.sqrt(self.remaining)


@dataclass
class SideProjection:
    lines: list[PlayerLine] = field(default_factory=list)

    @property
    def actual(self) -> float:
        return sum(l.actual for l in self.lines)

    @property
    def expected(self) -> float:
        return sum(l.expected for l in self.lines)

    @property
    def sd(self) -> float:
        return math.sqrt(sum(l.sd ** 2 for l in self.lines))

    @property
    def yet_to_play(self) -> int:
        return sum(1 for l in self.lines if l.state == "pre")

    @property
    def playing(self) -> int:
        return sum(1 for l in self.lines if l.state == "in")


def remaining_fraction(period: int, clock: str) -> float:
    """Share of regulation still to play from ESPN's period and clock."""
    try:
        mins, secs = (clock or "0:00").split(":")
        clock_min = int(mins) + int(secs) / 60.0
    except ValueError:
        clock_min = 0.0
    if period <= 0:
        return 1.0
    if period > 4:                       # overtime: nearly done
        return min(0.05, clock_min / 60.0)
    left = (4 - period) * QUARTER_MIN + clock_min
    return max(0.0, min(1.0, left / 60.0))


def game_states(week: int, season: int, ttl: float = 45) -> dict[str, GameState]:
    """team abbreviation (Sleeper spelling) -> state of that team's game this week."""
    # No User-Agent override. Measured 2026-09-13: ESPN returns 200 to httpx's
    # and curl's own default UAs, but 403 to the Chrome UA the rest of the HTTP
    # layer sends AND to a custom "mishpacha-fantasy/1.0". Passing None drops
    # the header so httpx supplies its default.
    data = get_json(ESPN_WEEK, params={"seasontype": 2, "week": week, "dates": season},
                    headers={"User-Agent": None},
                    ttl=ttl, timeout=20.0) or {}
    out: dict[str, GameState] = {}
    for ev in data.get("events", []):
        st = (ev.get("status") or {})
        kind = (st.get("type") or {}).get("state", "pre")
        detail = (st.get("type") or {}).get("shortDetail", "")
        if kind == "post":
            gs = GameState("post", 0.0, detail)
        elif kind == "in":
            gs = GameState("in", remaining_fraction(int(st.get("period") or 0),
                                                    st.get("displayClock", "0:00")), detail)
        else:
            gs = GameState("pre", 1.0, detail)
        for comp in (ev.get("competitions") or [{}])[0].get("competitors", []):
            abbr = ((comp.get("team") or {}).get("abbreviation") or "").upper()
            out[TEAM_ALIASES.get(abbr, abbr)] = gs
    return out


def project_side(
    slots: list[str],
    starters: list[str],
    starters_points: list[float],
    players: dict,                       # player_id -> sleeper Player
    weekly: dict,                        # player_id -> RawProjection
    games: dict[str, GameState],
) -> SideProjection:
    side = SideProjection()
    for slot, pid, pts in zip(slots, starters, starters_points):
        p = players.get(pid)
        pos = p.position if p else "?"
        team = (p.team if p else None) or (pid if pid and pid.isalpha() else None)
        w = weekly.get(pid)
        proj = float(w.points) if w else 0.0
        actual = float(pts or 0.0)
        g = games.get((team or "").upper())
        if pid in (None, "0", ""):
            line = PlayerLine(slot, "(empty)", pos, None, 0.0, 0.0, "bye", 0.0, "empty slot")
        elif g is None:
            line = PlayerLine(slot, p.name if p else pid, pos, team, actual, 0.0, "bye", 0.0, "no game")
        else:
            line = PlayerLine(slot, p.name if p else pid, pos, team, actual, proj,
                              g.state, g.remaining, g.detail)
        side.lines.append(line)
    return side


def win_probability(mine: SideProjection, theirs: SideProjection) -> float:
    diff = mine.expected - theirs.expected
    sd = math.sqrt(mine.sd ** 2 + theirs.sd ** 2)
    if sd <= 1e-9:
        return 1.0 if diff > 0 else (0.0 if diff < 0 else 0.5)
    return 0.5 * (1.0 + math.erf(diff / (sd * math.sqrt(2.0))))
