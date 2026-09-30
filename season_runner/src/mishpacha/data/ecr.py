"""FantasyPros Expert Consensus Rankings.

ECR is what 100+ analysts think; ADP is what drafters actually do. The GAP
between them is the cleanest market-inefficiency signal available: a player
ranked well above his ADP is someone the room is systematically undervaluing.

Uses the open `partners.` host, which needs no API key (the api.fantasypros.com
equivalent returns 403 without one).
"""
from __future__ import annotations

from dataclasses import dataclass

from .adp import normalize_name
from .http import get_json

URL = "https://partners.fantasypros.com/api/v1/consensus-rankings.php"


@dataclass(slots=True)
class ECREntry:
    name: str
    position: str
    team: str | None
    rank_ecr: int
    rank_ave: float
    rank_std: float
    rank_min: int
    rank_max: int
    pos_rank: str
    tier: int | None
    bye: int | None
    owned_avg: float | None

    @property
    def key(self) -> str:
        return normalize_name(self.name)


def fetch(
    position: str = "ALL", scoring: str = "PPR", year: int = 2026, ttl: float = 3600
) -> tuple[list[ECREntry], dict]:
    data = get_json(
        URL,
        params={
            "sport": "NFL", "year": year, "week": 0,
            "position": position, "type": "ST", "scoring": scoring,
        },
        ttl=ttl,
        timeout=45.0,
    )
    if not data or "players" not in data:
        raise RuntimeError("FantasyPros ECR fetch failed")

    out = []
    for p in data["players"]:
        try:
            bye = int(p.get("player_bye_week")) if p.get("player_bye_week") else None
        except (TypeError, ValueError):
            bye = None
        out.append(
            ECREntry(
                name=p.get("player_name", ""),
                position=(p.get("player_position_id") or "").upper().replace("DST", "DEF"),
                team=p.get("player_team_id"),
                rank_ecr=int(p.get("rank_ecr") or 999),
                rank_ave=float(p.get("rank_ave") or 999),
                rank_std=float(p.get("rank_std") or 0),
                rank_min=int(p.get("rank_min") or 999),
                rank_max=int(p.get("rank_max") or 999),
                pos_rank=p.get("pos_rank") or "",
                tier=int(p["tier"]) if p.get("tier") else None,
                bye=bye,
                owned_avg=p.get("player_owned_avg"),
            )
        )
    meta = {k: v for k, v in data.items() if k != "players"}
    return out, meta


def index(entries: list[ECREntry]) -> dict[str, ECREntry]:
    return {e.key: e for e in entries}
