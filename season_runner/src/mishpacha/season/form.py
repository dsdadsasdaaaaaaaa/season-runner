"""What a player is worth NOW, not what he was worth in August.

The board's points-per-game is a preseason projection. It was the only number
the waiver engine looked at, so by week 4 it was blind: on 2026-09-30 Kalif
Raymond was a free agent who had scored 16.4, 9.0 and 21.0 and the engine
priced him at 1.5 a game. For four straight weeks the verdict was "no add
clears the bar" while the rest of the league worked the wire.

The estimate blends three sources, each counted in "games of evidence":

    prior        the preseason ppg                      worth PRIOR_GAMES games
    outlook      this week's projection (current role)  worth OUTLOOK_GAMES games
    actuals      every game he has played this season   worth 1 game each

        ppg = (prior*Wp + outlook*Wo + sum(actuals)) / (Wp + Wo + n)

Early on the prior dominates; every game played moves weight onto what has
actually happened. The weights are not guesses: scripts/calibrate_form.py
replays 2025 and picks the pair that best predicts each player's NEXT four
games from what was knowable at the time (see the numbers next to the
constants). Weeks a player sat out are not zeros -- no stat line, no evidence;
availability is handled separately by the injury model.
"""
from __future__ import annotations

from ..data.http import get_json
from ..data.projections import ALL_POS, BASE, score_record

# Calibrated on 2025 by scripts/calibrate_form.py -- see its output in the
# comment block there before changing these.
PRIOR_GAMES = 3.0       # 2025 replay, 3700 player-weeks: prior-only MAE 4.418,
OUTLOOK_GAMES = 8.0     # this blend 3.707 (-16%). Heavier outlook gains <2.5% more and
                        # leans on final pre-game projections we will not have on a Tuesday.


def fetch_week_stats(season: str, week: int, ttl: float = 6 * 3600) -> list[dict]:
    """Raw stat lines for one finished week; same record shape as projections."""
    url = f"{BASE}/stats/nfl/{season}/{week}"
    params = dict(season_type="regular", order_by="pts_ppr")
    params["position[]"] = list(ALL_POS)
    return get_json(url, params=params, ttl=ttl, timeout=90.0) or []


def week_actuals(season: str, week: int, ttl: float = 6 * 3600) -> dict[str, float]:
    """player_id -> points scored that week under OUR rules, for players who
    actually appeared (gp >= 1). Team defenses always count."""
    out: dict[str, float] = {}
    for rec in fetch_week_stats(season, week, ttl=ttl):
        stats = rec.get("stats") or {}
        sp = score_record(rec)
        if sp is None:
            continue
        if sp.position != "DEF" and not (stats.get("gp") or stats.get("gms_active")):
            continue
        out[sp.player_id] = sp.points
    return out


def season_actuals(season: str, through_week: int) -> dict[str, list[float]]:
    """player_id -> list of points in each game played in weeks 1..through_week."""
    out: dict[str, list[float]] = {}
    for wk in range(1, through_week + 1):
        try:
            for pid, pts in week_actuals(season, wk).items():
                out.setdefault(pid, []).append(pts)
        except Exception:
            continue                     # a missing week is less evidence, not a crash
    return out


def current_ppg(
    prior_ppg: float,
    actuals: list[float] | None = None,
    outlook: float | None = None,
    prior_games: float = PRIOR_GAMES,
    outlook_games: float = OUTLOOK_GAMES,
) -> float:
    """Blend preseason prior, this week's projection and games played.

    `outlook` of None or <= 0 means no usable projection this week (bye, ruled
    out, or not in the feed) and that term is dropped rather than read as a
    forecast of zero.
    """
    actuals = actuals or []
    num = prior_ppg * prior_games + sum(actuals)
    den = prior_games + len(actuals)
    if outlook is not None and outlook > 0:
        num += outlook * outlook_games
        den += outlook_games
    return num / den if den > 0 else prior_ppg


def form_table(board, week: int, season: str) -> dict[str, float]:
    """player_id -> current ppg for every player on the board.

    `week` is the week being prepared for (sleeper.active_week()): actuals run
    through week-1 and the outlook is that week's projection.
    """
    from ..data.projections import weekly_projections

    actuals = season_actuals(season, week - 1) if week > 1 else {}
    try:
        outlook = {pid: p.points for pid, p in weekly_projections(season, week).items()}
    except Exception:
        outlook = {}
    out: dict[str, float] = {}
    for e in board:
        prior = e.points / max(e.games, 1)
        # A bye week projects 0 -- that is the schedule, not his role.
        look = None if e.bye == week else outlook.get(e.player_id)
        out[e.player_id] = current_ppg(prior, actuals.get(e.player_id), look)
    return out
