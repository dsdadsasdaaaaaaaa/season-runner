"""Streaming kickers, defenses and quarterbacks by weekly matchup.

This league's DEF scoring has NO yards-allowed component -- only points
allowed, plus turnovers and sacks. That makes the opponent's expected scoring
almost the entire story, and it is why streaming defenses works so well here:
you are essentially betting on which offense will be worst this week.

Kickers score by distance with NO penalty for missed field goals, which quietly
rewards high-volume big-leg kickers on offenses that stall in field-goal range
rather than the most accurate ones.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..data import projections as proj_mod


@dataclass(slots=True)
class StreamOption:
    player_id: str
    name: str
    position: str
    team: str | None
    proj: float
    opponent: str | None = None
    note: str = ""


def weekly_options(
    position: str,
    week: int,
    season: str = "2026",
    exclude: set[str] | None = None,
    limit: int = 12,
) -> list[StreamOption]:
    """Best available streamers at a position for a given week."""
    weekly = proj_mod.weekly_projections(season=season, week=week)
    ex = exclude or set()
    rows = [
        StreamOption(
            player_id=p.player_id, name=p.name, position=p.position,
            team=p.team, proj=p.points,
        )
        for p in weekly.values()
        if p.position == position.upper() and p.player_id not in ex
    ]
    rows.sort(key=lambda r: -r.proj)
    return rows[:limit]


def stream_plan(
    position: str,
    weeks: range,
    season: str = "2026",
    rostered: set[str] | None = None,
) -> dict[int, list[StreamOption]]:
    """Look ahead several weeks so we can pre-claim a streamer before the room
    notices the matchup. Waivers clear Wednesday here, so planning one week
    ahead is the difference between getting a guy for $1 and not at all."""
    return {
        wk: weekly_options(position, wk, season=season, exclude=rostered, limit=6)
        for wk in weeks
    }
