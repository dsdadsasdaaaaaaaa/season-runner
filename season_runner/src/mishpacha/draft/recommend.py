"""The recommendation engine: one call answers 'what do I do right now'."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import DRAFT_ROUNDS, MY_DRAFT_SLOT, NUM_TEAMS, STARTER_SLOTS
from ..model.board import BoardEntry
from .simulate import (
    OUR_CAPS,
    empirical_survival,
    SimPlayer,
    SimResult,
    replacement_ppg,
    simulate_candidates,
    to_sim_players,
)
from .state import DraftState


@dataclass
class Recommendation:
    pick_no: int
    round_: int
    primary: SimResult | None = None
    alternatives: list[SimResult] = field(default_factory=list)
    margin: float = 0.0
    confidence: float = 0.0
    survival: dict[str, float] = field(default_factory=dict)
    reasoning: list[str] = field(default_factory=list)
    queue: list[str] = field(default_factory=list)   # player_ids, best first

    @property
    def name(self) -> str:
        return self.primary.name if self.primary else "-"


class DraftBrain:
    """Holds the board and answers pick questions against a live draft state."""

    def __init__(
        self,
        board: list[BoardEntry],
        my_slot: int = MY_DRAFT_SLOT,
        pool_limit: int = 300,
        n_sims: int = 500,
    ):
        self.board = board
        self.entries = {e.player_id: e for e in board}
        self.players: list[SimPlayer] = to_sim_players(board, limit=pool_limit)
        self.by_id = {p.player_id: p for p in self.players}
        self.replacement = replacement_ppg(board)
        self.my_slot = my_slot
        self.n_sims = n_sims
        self.width = 8          # candidates simulated = up to 2x this

    # -- candidate generation -------------------------------------------
    def available(self, state: DraftState) -> list[SimPlayer]:
        return [p for p in self.players if p.player_id not in state.drafted]

    def candidates(self, state: DraftState, width: int = 8) -> list[SimPlayer]:
        """Players worth simulating: the best by ADP plus the best by value.

        Using both matters. ADP order alone misses genuine values the room is
        sleeping on; value order alone would have us reach for someone we could
        comfortably get two rounds later.

        Candidates are filtered against our OWN roster caps. Without this the
        engine reasons under one set of rules and acts under another: the
        simulation assumes future picks respect the caps, but the pick actually
        taken ignores them -- which is how a mock draft ended up rostering
        three tight ends against a cap of two.
        """
        avail = self.available(state)
        if not avail:
            return []

        counts = dict(state.me.positions)
        remaining = DRAFT_ROUNDS - len(state.me.players)

        # Every unfilled MANDATORY starting slot, counted with multiplicity.
        # This must cover all of STARTER_SLOTS, not just K and DEF: with
        # quarterback streaming valued at 17 ppg, no quarterback ever looked
        # worth a pick and a dress rehearsal produced a roster of
        # {RB 6, WR 5, TE 2, K 1, DEF 1} -- fifteen players and no legal
        # lineup. Streaming is a weekly tactic; you still have to own one.
        missing: list[str] = []
        for pos, need in STARTER_SLOTS.items():
            missing.extend([pos] * max(0, need - counts.get(pos, 0)))

        if missing and remaining <= len(missing):
            # No slack left: only positions we are legally short of.
            forced = [p for p in avail if p.position in set(missing)]
            if forced:
                avail = forced
        else:
            avail = [
                p for p in avail
                if counts.get(p.position, 0) < OUR_CAPS.get(p.position, 99)
            ] or self.available(state)

            # Defer kickers and defenses to the closing rounds.
            #
            # The objective values a bench skill player at his projection with
            # no upside term, so a deep flier scores ~0 marginal while an elite
            # defense scores +0.4 -- and the engine would spend a round-9 pick
            # on it. In reality that flier is a lottery ticket on a breakout,
            # and the defense can be had (or streamed) fifty picks later for
            # almost nothing. Every such pick the simulator proposed carried a
            # margin under 0.7 points, so deferring costs virtually nothing and
            # buys protection against a known blind spot.
            if remaining > len(missing) + 2:
                avail = [p for p in avail if p.position not in ("K", "DEF")] or avail

        by_adp = sorted(avail, key=lambda p: p.adp)[: width + 4]
        by_vor = sorted(
            avail, key=lambda p: -(self.entries[p.player_id].vor if p.player_id in self.entries else 0)
        )[: width + 4]
        seen, out = set(), []
        for p in by_adp + by_vor:
            if p.player_id not in seen:
                seen.add(p.player_id)
                out.append(p)
        return out[: width * 2]

    # -- the main call ---------------------------------------------------
    def recommend(self, state: DraftState, width: int | None = None,
                  n_sims: int | None = None) -> Recommendation:
        pick = state.on_the_clock_pick
        cands = self.candidates(state, width=width or self.width)
        rec = Recommendation(pick_no=pick, round_=state.current_round)
        if not cands:
            return rec

        results = simulate_candidates(
            players=self.players,
            taken=set(state.drafted),
            my_roster_ids=list(state.me.players),
            current_pick=pick,
            candidates=cands,
            n_sims=n_sims or self.n_sims,
            my_slot=self.my_slot,
            replacement=self.replacement,
            opponent_counts={sl: dict(t.positions) for sl, t in state.teams.items()},
        )
        rec.primary = results[0]
        rec.alternatives = results[1:6]
        rec.margin = round(results[0].mean_value - results[1].mean_value, 1) if len(results) > 1 else 0.0
        rec.confidence = results[0].win_rate
        rec.queue = [r.player_id for r in results]

        # Survival: what of this shortlist can we plausibly still get next time?
        # Measured with the same opponent model that produced the pick above,
        # so the advice and the decision cannot drift apart.
        nxt = state.next_pick_after(pick)
        if nxt:
            surv = empirical_survival(
                self.players, set(state.drafted), pick, nxt,
                n_sims=400, assume_we_take=rec.primary.player_id,
            )
            for r in results[:8]:
                if r.player_id in surv:
                    rec.survival[r.name] = surv[r.player_id]

        rec.reasoning = self._explain(state, results, rec)
        return rec

    def _explain(self, state: DraftState, results: list[SimResult], rec: Recommendation) -> list[str]:
        out: list[str] = []
        top = results[0]
        e = self.entries.get(top.player_id)
        nxt = state.next_pick_after(state.on_the_clock_pick)

        if e:
            out.append(
                f"{top.name} ({top.position}, {e.team or '--'}) - proj {e.points:.0f} pts, "
                f"VOR {e.vor:.0f} (rank {e.valuation.vor_rank}), tier {e.tier}, ADP {e.adp:.1f}"
            )
            if e.ecr_adp_gap and e.ecr_adp_gap > 6:
                out.append(
                    f"Experts rank him #{e.ecr_rank} but the room drafts him at {e.adp:.0f} "
                    f"- a {e.ecr_adp_gap:+.0f}-pick market discount."
                )
            if e.injury_status:
                out.append(f"Carrying an injury tag: {e.injury_status} (value already discounted).")

        if rec.margin > 0:
            out.append(
                f"Best in {rec.confidence:.0%} of {self.n_sims} simulated drafts, "
                f"+{rec.margin:.1f} season points over the next-best option."
            )

        if nxt:
            gap = nxt - state.on_the_clock_pick - 1
            surv = rec.survival.get(top.name)
            if surv is not None:
                out.append(
                    f"{gap} picks until our next selection (#{nxt}); "
                    f"{top.name} survives that gap only {surv:.0%} of the time."
                )
            safe = [n for n, s in rec.survival.items() if s > 0.6 and n != top.name]
            if safe:
                out.append(f"Likely still there at #{nxt}: {', '.join(safe[:4])}.")

        # picks cur+1 .. nxt-1 -- our own next pick is not opponent demand
        cur = state.on_the_clock_pick
        span = max(nxt - cur - 1, 0) if nxt else 12
        demand = state.positional_demand(cur + 1, span)
        hot = sorted(demand.items(), key=lambda kv: -kv[1])[:2]
        if hot:
            out.append("Positional demand before our next pick: " + ", ".join(f"{k} {v}" for k, v in hot) + ".")

        run = state.recent_position_run()
        if run:
            big = max(run.items(), key=lambda kv: kv[1])
            if big[1] >= 4:
                out.append(f"Run alert: {big[1]} of the last 6 picks were {big[0]}.")

        need = state.me.unfilled_starters()
        if need:
            out.append("Starting slots still empty: " + ", ".join(f"{k}x{v}" for k, v in need.items()) + ".")
        return out
