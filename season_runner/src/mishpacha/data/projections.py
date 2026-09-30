"""Season and weekly projections, scored under OUR league rules.

Source: Sleeper's undocumented projections API on api.sleeper.com (note: a
different host from the documented api.sleeper.app). It is the best available
feed for this project because it returns RAW PROJECTED STATS keyed by Sleeper
player_id -- meaning we apply this league's exact scoring ourselves and never
inherit another site's scoring assumptions, and we never have to name-match.

KNOWN LIMITATION (verified 2026-08-23): the season feed is complete for
QB/RB/WR/TE but PARTIAL for K and DEF --
  * K exposes only fgm_40_49 / fgm_50p / xpm / xpmiss; the short-FG buckets
    (0-19, 20-29, 30-39) are absent, so a from-scratch score would badly
    understate kickers.
  * DEF exposes sacks/INTs/fumbles/TDs but is missing most pts_allow_* buckets
    and reports a nonsensical gp of 1.0.
For those two positions we therefore fall back to the feed's own pts_ppr total
rather than computing from components. That is acceptable: K and DEF go in the
last rounds here (earliest DEF ADP 82.9, earliest K 130.0) and are streamed
weekly in-season, where the streaming model -- not the preseason projection --
carries the edge.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..model.scoring import score_line
from .http import get_json

BASE = "https://api.sleeper.com"
SKILL = ("QB", "RB", "WR", "TE")
ALL_POS = ("QB", "RB", "WR", "TE", "K", "DEF")

# Stat keys that are metadata, not production.
_NON_STAT = {"gp", "gms_active", "tm_def_snp", "tm_off_snp", "tm_st_snp"}

# Positions whose components are too incomplete to score from scratch.
FALLBACK_POSITIONS = frozenset({"K", "DEF"})


def _pos_params(positions=ALL_POS) -> list[tuple[str, str]]:
    return [("position[]", p) for p in positions]


def fetch_season(season: str = "2026", ttl: float = 21_600) -> list[dict]:
    """Raw season projection records for all fantasy positions."""
    url = f"{BASE}/projections/nfl/{season}"
    params = dict(season_type="regular", order_by="pts_ppr")
    # httpx needs repeated keys as a list value
    params["position[]"] = list(ALL_POS)
    return get_json(url, params=params, ttl=ttl, timeout=90.0) or []


def fetch_week(season: str = "2026", week: int = 1, ttl: float = 3600) -> list[dict]:
    """Raw weekly projection records -- the basis of in-season lineup setting."""
    url = f"{BASE}/projections/nfl/{season}/{week}"
    params = dict(season_type="regular", order_by="pts_ppr")
    params["position[]"] = list(ALL_POS)
    return get_json(url, params=params, ttl=ttl, timeout=90.0) or []


@dataclass(slots=True)
class RawProjection:
    player_id: str
    name: str
    position: str
    team: str | None
    stats: dict
    games: float
    points: float          # scored under OUR rules
    source_points: float   # the feed's own PPR total, for sanity-checking
    adp_ppr: float | None
    scored_from: str       # "components" or "source_total"


def _extract(rec: dict) -> tuple[str, str, str | None, str]:
    p = rec.get("player") or {}
    pid = str(rec.get("player_id") or "")
    pos = (p.get("fantasy_positions") or [None])[0] or ""
    name = " ".join(filter(None, [p.get("first_name"), p.get("last_name")])).strip()
    if not name:
        name = pid
    return pid, pos, p.get("team"), name


def score_record(rec: dict) -> RawProjection | None:
    """Convert one raw feed record into a scored projection."""
    pid, pos, team, name = _extract(rec)
    if not pid or pos not in ALL_POS:
        return None

    stats = {
        k: v for k, v in (rec.get("stats") or {}).items()
        if not k.startswith("adp") and not k.startswith("pts_") and k not in _NON_STAT
    }
    source_points = float((rec.get("stats") or {}).get("pts_ppr") or 0.0)
    games = float((rec.get("stats") or {}).get("gp") or 0.0)
    adp = (rec.get("stats") or {}).get("adp_ppr")

    if pos in FALLBACK_POSITIONS:
        pts, how = source_points, "source_total"
    else:
        pts, how = score_line(stats), "components"

    return RawProjection(
        player_id=pid, name=name, position=pos, team=team, stats=stats,
        games=games if games > 1 else 17.0,
        points=round(pts, 2), source_points=round(source_points, 2),
        adp_ppr=float(adp) if adp not in (None, 999.0) else None,
        scored_from=how,
    )


def season_projections(season: str = "2026", ttl: float = 21_600) -> dict[str, RawProjection]:
    """player_id -> scored season projection."""
    out: dict[str, RawProjection] = {}
    for rec in fetch_season(season, ttl=ttl):
        sp = score_record(rec)
        if sp and sp.points > 0:
            out[sp.player_id] = sp
    return out


def weekly_projections(
    season: str = "2026", week: int = 1, ttl: float = 3600
) -> dict[str, RawProjection]:
    out: dict[str, RawProjection] = {}
    for rec in fetch_week(season, week, ttl=ttl):
        sp = score_record(rec)
        if sp:
            out[sp.player_id] = sp
    return out


def scoring_agreement(projs: dict[str, RawProjection], position: str = "RB") -> dict:
    """Sanity check: our component scoring vs the feed's own PPR total.

    They should agree closely for skill positions since this league uses
    textbook full-PPR values. A large systematic gap means a scoring-key
    mismatch that would silently corrupt every valuation -- so this is checked
    rather than assumed.
    """
    rows = [p for p in projs.values() if p.position == position and p.scored_from == "components"]
    rows = sorted(rows, key=lambda p: -p.points)[:40]
    if not rows:
        return {}
    diffs = [p.points - p.source_points for p in rows]
    return {
        "position": position,
        "n": len(rows),
        "mean_diff": round(sum(diffs) / len(diffs), 2),
        "max_abs_diff": round(max(abs(d) for d in diffs), 2),
        "worst": max(rows, key=lambda p: abs(p.points - p.source_points)).name,
    }
