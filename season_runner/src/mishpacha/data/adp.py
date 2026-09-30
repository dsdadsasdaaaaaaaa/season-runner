"""Average Draft Position ingestion.

ADP is the backbone of draft strategy: it tells us when each player will
actually come off the board, which is what makes "wait vs. reach" answerable.

Primary source is Fantasy Football Calculator, which publishes ADP computed
from real mock/live drafts at our exact format (12-team PPR). Critically it
also publishes `stdev`, so the survival model uses measured dispersion rather
than an assumed curve -- and `bye`, which the roster evaluator needs.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .http import get_json

FFC_URL = "https://fantasyfootballcalculator.com/api/v1/adp/{scoring}"

_SUFFIXES = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b\.?", re.I)
_NONALPHA = re.compile(r"[^a-z ]")

# Sleeper uses team abbreviations that differ from some feeds.
TEAM_ALIASES = {"JAC": "JAX", "WSH": "WAS", "LA": "LAR", "OAK": "LV", "SD": "LAC", "STL": "LAR"}


def normalize_name(name: str) -> str:
    """Canonical form for cross-source player matching.

    Feeds disagree constantly on punctuation, suffixes, and accents
    ("Ja'Marr Chase" / "JaMarr Chase", "Marvin Harrison Jr." / "Marvin
    Harrison"). Matching on a normalized key avoids silently dropping players.
    """
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace(".", "").replace("'", "").replace("-", " ")
    s = _SUFFIXES.sub("", s)
    s = _NONALPHA.sub("", s)
    return " ".join(s.split())


@dataclass(slots=True)
class ADPEntry:
    name: str
    position: str
    team: str | None
    adp: float
    stdev: float
    high: int | None = None
    low: int | None = None
    times_drafted: int | None = None
    bye: int | None = None

    @property
    def key(self) -> str:
        return normalize_name(self.name)


def fetch_ffc(
    scoring: str = "ppr", teams: int | None = None, year: int = 2026, ttl: float = 3600
) -> tuple[list[ADPEntry], dict]:
    """Fetch ADP from Fantasy Football Calculator. Returns (entries, meta).

    `teams` defaults to THIS league's size. It used to be hardcoded to 12, which
    silently survived the league being resized to 10 -- the board was then built
    from twelve-team ADP while the engine simulated a ten-team draft. Players go
    later in a smaller room, so the engine believed the board would empty faster
    than it does and reached for players it could have waited on.

    Callers should also check `meta["teams"]` against what they asked for: the
    API falls back to another size rather than erroring when the requested one
    has no data (2025 has no ten-team PPR sample, for instance).
    """
    from ..config import NUM_TEAMS
    teams = NUM_TEAMS if teams is None else teams
    data = get_json(
        FFC_URL.format(scoring=scoring),
        params={"teams": teams, "year": year},
        ttl=ttl,
    )
    if not data or data.get("status") != "Success":
        raise RuntimeError(f"FFC ADP fetch failed: {(data or {}).get('status')}")

    entries = []
    for p in data.get("players", []):
        team = p.get("team")
        entries.append(
            ADPEntry(
                name=p.get("name", ""),
                position=(p.get("position") or "").upper().replace("PK", "K"),
                team=TEAM_ALIASES.get(team, team),
                adp=float(p.get("adp") or 999),
                stdev=float(p.get("stdev") or 0),
                high=p.get("high"),
                low=p.get("low"),
                times_drafted=p.get("times_drafted"),
                bye=p.get("bye"),
            )
        )
    entries.sort(key=lambda e: e.adp)
    return entries, data.get("meta", {})


def index_by_name(entries: list[ADPEntry]) -> dict[str, ADPEntry]:
    return {e.key: e for e in entries}


def match_to_sleeper(
    entries: list[ADPEntry], sleeper_players: dict
) -> tuple[dict[str, ADPEntry], list[ADPEntry]]:
    """Join ADP rows onto Sleeper player_ids.

    Returns (player_id -> ADPEntry, unmatched). Matching is by normalized name
    plus position, falling back to name-only. Unmatched entries are surfaced
    rather than swallowed, because a silently-dropped ADP row means a player
    the draft engine believes nobody wants.
    """
    by_np: dict[tuple[str, str], str] = {}
    by_n: dict[str, list[str]] = {}
    for pid, p in sleeper_players.items():
        pos = getattr(p, "position", None) or (p.get("position") if isinstance(p, dict) else None)
        nm = getattr(p, "name", None) or (p.get("full_name") if isinstance(p, dict) else None)
        if not nm or not pos:
            continue
        k = normalize_name(nm)
        by_np[(k, pos)] = pid
        by_n.setdefault(k, []).append(pid)

    # Team defenses are keyed by bare team abbreviation in Sleeper
    # ("HOU" -> "Houston Texans"), while ADP feeds spell them out
    # ("Houston Defense"). Match those on team code, not name.
    def_ids = {
        (getattr(p, "team", None) or pid): pid
        for pid, p in sleeper_players.items()
        if (getattr(p, "position", None) or (p.get("position") if isinstance(p, dict) else None)) == "DEF"
    }

    matched: dict[str, ADPEntry] = {}
    unmatched: list[ADPEntry] = []
    for e in entries:
        pid = None
        if e.position == "DEF":
            if e.team:
                pid = def_ids.get(defense_key(e.team))
        else:
            pid = by_np.get((e.key, e.position))
            if pid is None:
                cands = by_n.get(e.key) or []
                pid = cands[0] if len(cands) == 1 else None
        if pid:
            matched[pid] = e
        else:
            unmatched.append(e)
    return matched, unmatched


def defense_key(team: str) -> str:
    """Sleeper keys team defenses by bare team abbreviation."""
    return TEAM_ALIASES.get(team.upper(), team.upper())
