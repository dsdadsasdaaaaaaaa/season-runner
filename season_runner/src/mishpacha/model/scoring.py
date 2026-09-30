"""Apply Season Runner scoring to stat lines.

Two jobs:
  1. score_line()  -- exact points for a realized or projected stat line.
  2. from_generic() -- convert the "standard" projection shape most public
     sources publish (pass yds/TD/INT, rush yds/TD, rec/rec yds/TD, fumbles)
     into league points, so any source can be normalized to OUR scoring rather
     than trusting a source's own "fantasy points" column, which is almost
     always half-PPR or standard and would silently corrupt every valuation.
"""
from __future__ import annotations

from ..config import SCORING

# Aliases: public projection feeds use wildly inconsistent field names.
ALIASES: dict[str, str] = {
    # passing
    "passing_yards": "pass_yd", "pass_yards": "pass_yd", "py": "pass_yd",
    "passing_tds": "pass_td", "pass_tds": "pass_td", "ptd": "pass_td",
    "interceptions": "pass_int", "ints": "pass_int", "int_thrown": "pass_int",
    # rushing
    "rushing_yards": "rush_yd", "rush_yards": "rush_yd", "ry": "rush_yd",
    "rushing_tds": "rush_td", "rush_tds": "rush_td", "rtd": "rush_td",
    # receiving
    "receptions": "rec", "receiving_yards": "rec_yd", "rec_yards": "rec_yd",
    "receiving_tds": "rec_td", "rec_tds": "rec_td",
    # misc
    "fumbles_lost": "fum_lost", "fl": "fum_lost",
}


def normalize_keys(line: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    for k, v in line.items():
        if v is None:
            continue
        key = ALIASES.get(k, k)
        try:
            out[key] = out.get(key, 0.0) + float(v)
        except (TypeError, ValueError):
            continue
    return out


def score_line(line: dict, scoring: dict[str, float] | None = None) -> float:
    """Fantasy points for a stat line under this league's rules."""
    rules = scoring or SCORING
    stats = normalize_keys(line)
    return round(sum(rules.get(k, 0.0) * v for k, v in stats.items()), 3)


def score_breakdown(line: dict, scoring: dict[str, float] | None = None) -> dict[str, float]:
    """Per-category point contributions -- for explaining a recommendation."""
    rules = scoring or SCORING
    stats = normalize_keys(line)
    return {
        k: round(rules[k] * v, 3)
        for k, v in stats.items()
        if rules.get(k) and abs(rules[k] * v) > 1e-9
    }


def from_generic(
    *,
    pass_yd: float = 0, pass_td: float = 0, pass_int: float = 0,
    rush_yd: float = 0, rush_td: float = 0,
    rec: float = 0, rec_yd: float = 0, rec_td: float = 0,
    fum_lost: float = 0, two_pt: float = 0,
) -> float:
    """Score the common projection shape. Keyword-only to prevent order bugs."""
    return score_line({
        "pass_yd": pass_yd, "pass_td": pass_td, "pass_int": pass_int,
        "rush_yd": rush_yd, "rush_td": rush_td,
        "rec": rec, "rec_yd": rec_yd, "rec_td": rec_td,
        "fum_lost": fum_lost, "rec_2pt": two_pt,
    })


def defense_points(
    *, sacks: float = 0, ints: float = 0, fum_rec: float = 0, ff: float = 0,
    def_td: float = 0, st_td: float = 0, safeties: float = 0,
    blocked_kicks: float = 0, points_allowed: float = 0,
) -> float:
    """Team DEF scoring. This league has NO yards-allowed component -- points
    allowed is the only bucket, which makes opponent implied team total the
    dominant predictor for streaming."""
    pa_bucket = points_allowed_bucket(points_allowed)
    return score_line({
        "sack": sacks, "int": ints, "fum_rec": fum_rec, "ff": ff,
        "def_td": def_td, "st_td": st_td, "safe": safeties,
        "blk_kick": blocked_kicks, pa_bucket: 1,
    })


def points_allowed_bucket(pa: float) -> str:
    if pa <= 0:   return "pts_allow_0"
    if pa <= 6:   return "pts_allow_1_6"
    if pa <= 13:  return "pts_allow_7_13"
    if pa <= 20:  return "pts_allow_14_20"
    if pa <= 27:  return "pts_allow_21_27"
    if pa <= 34:  return "pts_allow_28_34"
    return "pts_allow_35p"


def kicker_points(
    *, xpm: float = 0, xpmiss: float = 0,
    fg_0_19: float = 0, fg_20_29: float = 0, fg_30_39: float = 0,
    fg_40_49: float = 0, fg_50_59: float = 0, fg_60p: float = 0,
) -> float:
    """Kickers score by distance here, with NO penalty for missed FGs (only
    missed XPs). That rewards high-volume big-leg kickers on offenses that
    stall in FG range -- and removes the usual accuracy risk entirely."""
    return score_line({
        "xpm": xpm, "xpmiss": xpmiss,
        "fgm_0_19": fg_0_19, "fgm_20_29": fg_20_29, "fgm_30_39": fg_30_39,
        "fgm_40_49": fg_40_49, "fgm_50_59": fg_50_59, "fgm_60p": fg_60p,
    })
