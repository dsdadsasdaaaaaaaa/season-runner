"""Sleeper read API client.

Sleeper's documented v1 API is read-only and unauthenticated. Docs say to stay
under ~1000 calls/minute; we are nowhere near that, but the cache TTLs below
are tuned so a live draft poll loop stays polite while still reacting fast.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .http import get_json

BASE = "https://api.sleeper.app/v1"

# TTLs in seconds. Draft picks get ~0 because during a live draft we need the
# board within a second or two of each pick landing.
TTL_STATIC = 86_400     # player universe, rarely changes intra-day
TTL_LEAGUE = 300        # league settings, membership
TTL_ROSTER = 60
TTL_LIVE = 0            # draft picks, in-game scoring


# --------------------------------------------------------------------------
# Platform state
# --------------------------------------------------------------------------
def nfl_state() -> dict:
    return get_json(f"{BASE}/state/nfl", ttl=60)


def current_week() -> int:
    """The week Sleeper is SHOWING -- i.e. the one whose scores are on screen.

    Use this for the live matchup panel only. It lags: once Monday night ends,
    `display_week` stays on the finished week until Wednesday, so on Tuesday
    it reads 2 while the league is already preparing week 3.
    """
    return int(nfl_state().get("display_week") or 1)


def active_week() -> int:
    """The week roster decisions apply to -- the next one to be played.

    Every add, drop and lineup change is for this week, never for the one
    that just finished. Sleeper's own `week` (mirrored by `leg`) rolls over
    as soon as the last game ends; `display_week` does not.

    This matters most on Tuesday night, when the waiver job runs: measured
    2026-09-22, state was week=3 leg=3 display_week=2, so the waiver engine
    was valuing free agents against week 2 -- a week already in the books,
    with the wrong byes and the wrong injuries.
    """
    st = nfl_state()
    return int(st.get("week") or st.get("leg") or st.get("display_week") or 1)


def is_regular_season() -> bool:
    return nfl_state().get("season_type") == "regular"


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------
def user(username_or_id: str) -> dict | None:
    try:
        return get_json(f"{BASE}/user/{username_or_id}", ttl=TTL_LEAGUE)
    except RuntimeError:
        return None


def user_leagues(user_id: str, season: str = "2026") -> list[dict]:
    return get_json(f"{BASE}/user/{user_id}/leagues/nfl/{season}", ttl=TTL_LEAGUE) or []


# --------------------------------------------------------------------------
# League
# --------------------------------------------------------------------------
def league(league_id: str) -> dict:
    return get_json(f"{BASE}/league/{league_id}", ttl=TTL_LEAGUE)


def league_users(league_id: str) -> list[dict]:
    return get_json(f"{BASE}/league/{league_id}/users", ttl=TTL_LEAGUE) or []


def rosters(league_id: str, fresh: bool = False) -> list[dict]:
    """League rosters. `fresh=True` defeats both our cache and the CDN in front
    of api.sleeper.app, which serves a stale copy for minutes: right after the
    Bears were added on 2026-09-30 the plain read still listed the Eagles, and
    the lineup engine planned around a player no longer on the team."""
    if fresh:
        import time
        return get_json(f"{BASE}/league/{league_id}/rosters",
                        params={"_": int(time.time() * 1000)},
                        headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
                        ttl=0) or []
    return get_json(f"{BASE}/league/{league_id}/rosters", ttl=TTL_ROSTER) or []


def matchups(league_id: str, week: int) -> list[dict]:
    return get_json(f"{BASE}/league/{league_id}/matchups/{week}", ttl=TTL_LIVE) or []


def transactions(league_id: str, week: int) -> list[dict]:
    return get_json(f"{BASE}/league/{league_id}/transactions/{week}", ttl=TTL_ROSTER) or []


def league_drafts(league_id: str) -> list[dict]:
    return get_json(f"{BASE}/league/{league_id}/drafts", ttl=TTL_LEAGUE) or []


def winners_bracket(league_id: str) -> list[dict]:
    return get_json(f"{BASE}/league/{league_id}/winners_bracket", ttl=TTL_ROSTER) or []


# --------------------------------------------------------------------------
# Draft
# --------------------------------------------------------------------------
def draft(draft_id: str, ttl: float = TTL_LEAGUE) -> dict:
    return get_json(f"{BASE}/draft/{draft_id}", ttl=ttl)


def draft_picks(draft_id: str, ttl: float = TTL_LIVE) -> list[dict]:
    """Picks made so far, in order. Empty list before the draft starts."""
    return get_json(f"{BASE}/draft/{draft_id}/picks", ttl=ttl) or []


def draft_traded_picks(draft_id: str) -> list[dict]:
    return get_json(f"{BASE}/draft/{draft_id}/traded_picks", ttl=TTL_LEAGUE) or []


def draft_slot_map(draft_id: str) -> dict[int, str]:
    """slot -> user_id, from the draft's draft_order once the commish sets it."""
    d = draft(draft_id)
    order = d.get("draft_order") or {}
    return {int(slot): uid for uid, slot in order.items()}


def slot_to_roster(draft_id: str, ttl: float = TTL_LEAGUE) -> dict[int, int]:
    d = draft(draft_id, ttl=ttl)
    return {int(k): v for k, v in (d.get("slot_to_roster_id") or {}).items()}


def my_roster_id(league_id: str, user_id: str) -> int | None:
    for r in rosters(league_id):
        if r.get("owner_id") == user_id:
            return r.get("roster_id")
        if user_id in (r.get("co_owners") or []):
            return r.get("roster_id")
    return None


# --------------------------------------------------------------------------
# Players
# --------------------------------------------------------------------------
def all_players(ttl: float = TTL_STATIC) -> dict[str, dict]:
    """The full NFL player universe (~5MB). Cached hard; call sparingly."""
    return get_json(f"{BASE}/players/nfl", ttl=ttl, timeout=120.0) or {}


def trending(kind: str = "add", lookback_hours: int = 24, limit: int = 50) -> list[dict]:
    """Crowd signal: most-added or most-dropped players across all of Sleeper."""
    return get_json(
        f"{BASE}/players/nfl/trending/{kind}",
        params={"lookback_hours": lookback_hours, "limit": limit},
        ttl=900,
    ) or []


# --------------------------------------------------------------------------
# Normalized views
# --------------------------------------------------------------------------
FANTASY_POSITIONS = frozenset({"QB", "RB", "WR", "TE", "K", "DEF"})


@dataclass(slots=True)
class Player:
    player_id: str
    name: str
    position: str
    team: str | None
    age: int | None
    years_exp: int | None
    injury_status: str | None
    depth_chart_position: str | None
    depth_chart_order: int | None
    search_rank: int | None
    status: str | None
    number: int | None = None

    @property
    def is_injured(self) -> bool:
        return bool(self.injury_status) and self.injury_status not in ("Questionable",)

    @property
    def is_out_long_term(self) -> bool:
        return self.injury_status in ("IR", "PUP", "Sus", "NA", "DNR")


def fantasy_players(players: dict[str, dict] | None = None) -> dict[str, Player]:
    """Filter the universe down to fantasy-relevant, currently-rostered players."""
    raw = players if players is not None else all_players()
    out: dict[str, Player] = {}

    for pid, p in raw.items():
        if not isinstance(p, dict):
            continue
        pos = p.get("position")
        fpos = set(p.get("fantasy_positions") or [])
        if pos not in FANTASY_POSITIONS and not (fpos & FANTASY_POSITIONS):
            continue
        if pos not in FANTASY_POSITIONS:
            pos = next(iter(fpos & FANTASY_POSITIONS))

        # Team defenses come through with player_id == team abbreviation.
        name = (
            p.get("full_name")
            or " ".join(filter(None, [p.get("first_name"), p.get("last_name")]))
            or pid
        )

        out[pid] = Player(
            player_id=pid,
            name=name.strip(),
            position=pos,
            team=p.get("team"),
            age=p.get("age"),
            years_exp=p.get("years_exp"),
            injury_status=p.get("injury_status"),
            depth_chart_position=p.get("depth_chart_position"),
            depth_chart_order=p.get("depth_chart_order"),
            search_rank=p.get("search_rank"),
            status=p.get("status"),
            number=p.get("number"),
        )
    return out


def active_pool(
    players: dict[str, Player] | None = None,
    keep_ids: set[str] | None = None,
) -> dict[str, Player]:
    """Players plausibly relevant to a 2026 draft.

    Deliberately permissive on status. Sleeper marks plenty of draftable
    players "Inactive" (roster churn, preseason designations) -- Jayden Higgins
    carried ADP 129.8 while flagged Inactive. Dropping those would make the
    engine believe nobody wants a player the room is actively drafting, so the
    only hard exclusion is Retired, plus anyone with no NFL team at all.

    `keep_ids` force-includes players (e.g. everyone carrying an ADP).
    """
    pool = players if players is not None else fantasy_players()
    keep = keep_ids or set()
    return {
        pid: p
        for pid, p in pool.items()
        if pid in keep
        or ((p.team or p.position == "DEF") and p.status != "Retired")
    }
