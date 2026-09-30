"""The draft board: fuse every source into one valued, ADP-aware player list.

Sources and why each is here:
  * Sleeper projections  -> projected POINTS, scored under our exact rules.
  * Sleeper adp_ppr      -> how OUR room will actually draft. This league plays
                            on Sleeper with autopick enabled, so Sleeper's own
                            ADP is the most predictive signal we have for what
                            our cousins (and the autopicker) will do.
  * FFC ADP              -> ADP measured across 7,479 real 12-team PPR drafts,
                            crucially including a per-player `stdev` so the
                            survival model uses MEASURED dispersion.
  * FantasyPros ECR      -> 103 experts: consensus rank, expert tiers, and
                            rank_std (how much the experts disagree).

The ADP blend leans on Sleeper because platform-specific behavior beats generic
market data for predicting one specific room, but keeps FFC for its dispersion.
The ECR-vs-ADP gap is retained as an explicit value signal.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import NUM_TEAMS
from ..data import adp as adp_mod
from ..data import ecr as ecr_mod
from ..data import projections as proj_mod
from ..data import sleeper as sleeper_mod
from .value import Projection, Valuation, valuate

# Weight on Sleeper's own ADP vs. FantasyFootballCalculator's.
SLEEPER_ADP_WEIGHT = 0.6


@dataclass(slots=True)
class BoardEntry:
    valuation: Valuation
    adp: float
    adp_sd: float
    sleeper_adp: float | None
    ffc_adp: float | None
    ecr_rank: int | None
    ecr_tier: int | None
    ecr_std: float | None
    bye: int | None
    injury_status: str | None
    times_drafted: int | None = None

    @property
    def player_id(self) -> str: return self.valuation.player_id
    @property
    def name(self) -> str: return self.valuation.name
    @property
    def position(self) -> str: return self.valuation.position
    @property
    def team(self) -> str | None: return self.valuation.projection.team
    @property
    def games(self) -> float: return self.valuation.projection.games
    @property
    def points(self) -> float: return self.valuation.projection.points
    @property
    def vor(self) -> float: return self.valuation.adjusted_vor
    @property
    def tier(self) -> int: return self.valuation.tier

    @property
    def adp_value(self) -> float:
        """How many picks of value the market is leaving on the table.

        Positive means the player's VOR-implied draft slot is EARLIER than his
        ADP -- i.e. he is available later than he should be. This is the number
        that finds sleepers.
        """
        return round(self.adp - self.valuation.vor_rank, 1)

    @property
    def ecr_adp_gap(self) -> float | None:
        """Experts vs. the market. Positive = experts like him more than the
        room does, so he can be had at a discount."""
        if self.ecr_rank is None:
            return None
        return round(self.adp - self.ecr_rank, 1)


def build_board(
    season: str = "2026",
    ttl: float = 3600,
    apply_risk: bool = True,
    adp_year: int | None = None,
    use_ecr: bool = True,
) -> tuple[list[BoardEntry], dict]:
    """Assemble the full valued board. Returns (entries sorted by VOR, diagnostics)."""
    diag: dict = {}

    projs = proj_mod.season_projections(season, ttl=ttl)
    diag["projections"] = len(projs)

    players = sleeper_mod.fantasy_players()
    pool = sleeper_mod.active_pool(players, keep_ids=set(projs))
    diag["pool"] = len(pool)

    ffc_entries, ffc_meta = adp_mod.fetch_ffc(

        teams=NUM_TEAMS, year=int(season), ttl=ttl)

    # The API substitutes another league size when the requested one has no

    # sample (2025 has no ten-team PPR data), so surface that rather than

    # silently modelling the wrong room.

    if ffc_meta.get("teams") and int(ffc_meta["teams"]) != NUM_TEAMS:

        diag["adp_size_mismatch"] = (

            f"requested {NUM_TEAMS}-team ADP, received {ffc_meta['teams']}-team")
    ffc_by_pid, ffc_unmatched = adp_mod.match_to_sleeper(ffc_entries, pool)
    diag["ffc"] = {"entries": len(ffc_entries), "matched": len(ffc_by_pid),
                   "unmatched": [e.name for e in ffc_unmatched], "meta": ffc_meta}

    # ECR is only published for the current season; historical backtests run
    # without it and lean on ADP dispersion alone.
    if use_ecr:
        ecr_entries, ecr_meta = ecr_mod.fetch(year=int(season), ttl=ttl)
    else:
        ecr_entries, ecr_meta = [], {}
    ecr_idx = ecr_mod.index(ecr_entries)
    diag["ecr"] = {"entries": len(ecr_entries),
                   "experts": ecr_meta.get("total_experts"),
                   "updated": ecr_meta.get("last_updated")}

    # Team-defense ECR keys off team code, same as ADP.
    ecr_def = {e.team: e for e in ecr_entries if e.position == "DEF" and e.team}

    max_pick = NUM_TEAMS * 15
    entries: list[BoardEntry] = []
    projections: list[Projection] = []
    meta: dict[str, dict] = {}

    for pid, sp in projs.items():
        p = pool.get(pid)
        ffc = ffc_by_pid.get(pid)

        if sp.position == "DEF":
            ec = ecr_def.get(sp.team)
        else:
            ec = ecr_idx.get(adp_mod.normalize_name(sp.name))
            if ec and ec.position != sp.position:
                ec = None

        # --- blend ADP -------------------------------------------------
        s_adp = sp.adp_ppr
        f_adp = ffc.adp if ffc else None
        if s_adp and f_adp:
            adp = SLEEPER_ADP_WEIGHT * s_adp + (1 - SLEEPER_ADP_WEIGHT) * f_adp
        else:
            adp = s_adp or f_adp or float(max_pick)

        # Measured dispersion where we have it, modelled where we do not.
        #
        # Deliberately NOT falling back to ECR rank_std: expert rank dispersion
        # and draft-position dispersion are different quantities on different
        # scales, and borrowing one for the other produced entries like a
        # fullback at "ADP 180, sd 51" -- three sigma from being drafted in our
        # first round. Roughly 4% of simulated drafts burned an early pick on
        # one of those phantoms.
        from ..draft.survival import default_adp_sd
        if ffc and ffc.stdev:
            adp_sd = ffc.stdev
        else:
            adp_sd = default_adp_sd(adp)

        # A player with no ADP at all sits at the sentinel. Nobody is drafting
        # him early, so his dispersion must not let him drift up the board.
        if not ffc and (s_adp is None or adp >= max_pick - 1e-6):
            adp_sd = min(adp_sd, max(1.0, adp / 12.0))

        bye = (ffc.bye if ffc else None) or (ec.bye if ec else None)

        projections.append(
            Projection(
                player_id=pid, name=sp.name, position=sp.position, team=sp.team,
                points=sp.points, games=sp.games,
                adp=adp, adp_sd=adp_sd, bye_week=bye,
                injury_status=p.injury_status if p else None,
                sources={"scored_from": sp.scored_from},
            )
        )
        meta[pid] = {
            "adp": round(adp, 1), "adp_sd": round(adp_sd, 2),
            "sleeper_adp": s_adp, "ffc_adp": f_adp,
            "ecr_rank": ec.rank_ecr if ec else None,
            "ecr_tier": ec.tier if ec else None,
            "ecr_std": ec.rank_std if ec else None,
            "bye": bye,
            "injury": p.injury_status if p else None,
            "times_drafted": ffc.times_drafted if ffc else None,
        }

    vals = valuate(projections, apply_risk=apply_risk)
    for v in vals:
        m = meta[v.player_id]
        entries.append(
            BoardEntry(
                valuation=v, adp=m["adp"], adp_sd=m["adp_sd"],
                sleeper_adp=m["sleeper_adp"], ffc_adp=m["ffc_adp"],
                ecr_rank=m["ecr_rank"], ecr_tier=m["ecr_tier"], ecr_std=m["ecr_std"],
                bye=m["bye"], injury_status=m["injury"], times_drafted=m["times_drafted"],
            )
        )

    diag["board_size"] = len(entries)
    diag["ecr_matched"] = sum(1 for e in entries if e.ecr_rank is not None)
    return entries, diag


def draftable(entries: list[BoardEntry], limit_adp: float = 250.0) -> list[BoardEntry]:
    """Players realistically in play given the league's size and length."""
    return [e for e in entries if e.adp <= limit_adp]
