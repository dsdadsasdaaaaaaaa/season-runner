"""Live draft board state: who's gone, who's up, what everyone needs."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import (
    DRAFT_ROUNDS,
    FLEX_ELIGIBLE,
    FLEX_SLOTS,
    MAX_ROSTER_SHAPE,
    MY_DRAFT_SLOT,
    NUM_TEAMS,
    STARTER_SLOTS,
    pick_number,
    slot_for_pick,
)


@dataclass(slots=True)
class TeamState:
    slot: int
    roster_id: int | None = None
    user_id: str | None = None
    label: str = ""
    players: list[str] = field(default_factory=list)          # player_ids
    positions: dict[str, int] = field(default_factory=dict)   # pos -> count

    def add(self, player_id: str, position: str) -> None:
        self.players.append(player_id)
        self.positions[position] = self.positions.get(position, 0) + 1

    def starters_filled(self) -> dict[str, int]:
        """How many required starter slots this team has covered."""
        filled = {}
        for pos, need in STARTER_SLOTS.items():
            filled[pos] = min(self.positions.get(pos, 0), need)
        return filled

    def flex_filled(self) -> int:
        surplus = sum(
            max(0, self.positions.get(p, 0) - STARTER_SLOTS.get(p, 0))
            for p in FLEX_ELIGIBLE
        )
        return min(surplus, FLEX_SLOTS)

    def unfilled_starters(self) -> dict[str, int]:
        """Required starter slots still empty -- the strongest signal of what
        this team will draft next."""
        out = {}
        for pos, need in STARTER_SLOTS.items():
            missing = need - self.positions.get(pos, 0)
            if missing > 0:
                out[pos] = missing
        flex_missing = FLEX_SLOTS - self.flex_filled()
        if flex_missing > 0:
            out["FLEX"] = flex_missing
        return out

    def is_full_at(self, position: str) -> bool:
        return self.positions.get(position, 0) >= MAX_ROSTER_SHAPE.get(position, 99)

    def needs_score(self, position: str) -> float:
        """0..1 urgency that this team takes `position` next. Used to predict
        opponent behavior, which drives our survival probabilities."""
        unfilled = self.unfilled_starters()
        if self.is_full_at(position):
            return 0.0
        score = 0.0
        if position in unfilled:
            score += 0.6
        if position in FLEX_ELIGIBLE and unfilled.get("FLEX"):
            score += 0.25
        # Late-draft K/DEF urgency ramps hard once starters are otherwise set.
        if position in ("K", "DEF"):
            if len(self.players) >= DRAFT_ROUNDS - 3 and position in unfilled:
                score += 0.5
            else:
                score -= 0.4
        return max(0.0, min(1.0, score))


@dataclass
class DraftState:
    num_teams: int = NUM_TEAMS
    rounds: int = DRAFT_ROUNDS
    my_slot: int = MY_DRAFT_SLOT
    picks: list[dict] = field(default_factory=list)
    drafted: set[str] = field(default_factory=set)
    teams: dict[int, TeamState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.teams:
            self.teams = {s: TeamState(slot=s) for s in range(1, self.num_teams + 1)}

    # -- ingest ----------------------------------------------------------
    def apply_picks(self, raw_picks: list[dict], positions: dict[str, str]) -> int:
        """Sync from Sleeper's /draft/{id}/picks payload. Returns new-pick count.

        Rebuilds state from the feed rather than appending to it. Sleeper sends
        the FULL pick list on every poll, and incremental application corrupted
        state permanently in two demonstrated ways:

          * A commissioner undoing pick #5 and re-drafting a different player
            was skipped entirely (the pick_no was already seen), leaving a
            player marked drafted who was actually available -- and the real
            pick invisible. The engine, or a pre-set autopick queue, would then
            happily recommend someone already gone.
          * The same player re-delivered under a new pick_no was added twice,
            inflating picks_made and shifting `is_my_turn` onto the wrong picks
            for the remainder of the draft.

        A full rebuild is one row per pick and immune to both.
        """
        before = len(self.picks)

        cleaned: dict[int, dict] = {}
        for rp in raw_picks:
            pid = rp.get("player_id")
            pick_no = rp.get("pick_no")
            if not pid or pick_no is None:
                continue
            cleaned[int(pick_no)] = rp          # later entry wins on replacement

        # A player re-picked at a different slot should appear once, at his
        # latest pick_no.
        latest_for_player: dict[str, int] = {}
        for pick_no, rp in sorted(cleaned.items()):
            latest_for_player[rp["player_id"]] = pick_no

        self.picks = []
        self.drafted = set()
        self.teams = {s: TeamState(slot=s) for s in range(1, self.num_teams + 1)}

        for pick_no, rp in sorted(cleaned.items()):
            pid = rp["player_id"]
            if latest_for_player.get(pid) != pick_no:
                continue                        # stale duplicate of a moved pick
            slot = rp.get("draft_slot") or slot_for_pick(pick_no, self.num_teams)
            pos = positions.get(pid) or (rp.get("metadata") or {}).get("position") or "?"
            # Stash the resolved position. `recent_position_run` used to
            # re-derive it from metadata alone, so a feed that omitted
            # metadata.position gave correct team rosters but a blind run
            # detector -- and the live "Run alert" line would silently vanish
            # at exactly the moment a positional run was under way.
            rp = dict(rp)
            rp["_resolved_position"] = pos
            self.picks.append(rp)
            self.drafted.add(pid)
            self.teams.setdefault(slot, TeamState(slot=slot)).add(pid, pos)

        return max(0, len(self.picks) - before)

    # -- position in the draft -------------------------------------------
    @property
    def picks_made(self) -> int:
        return len(self.picks)

    @property
    def highest_pick(self) -> int:
        return max((p.get("pick_no") or 0) for p in self.picks) if self.picks else 0

    @property
    def is_synced(self) -> bool:
        """False when the feed has holes -- picks 1..N with one missing.

        Sleeper occasionally serves a lagging snapshot. Deriving the clock from
        a COUNT rather than the highest pick made the engine mis-identify whose
        turn it was: a feed of picks 1-19 missing #7 reported pick 19 on the
        clock and issued a full recommendation for a selection already made,
        with #7's player still counted as available.
        """
        return self.highest_pick == len(self.picks)

    @property
    def on_the_clock_pick(self) -> int:
        return self.highest_pick + 1

    @property
    def current_round(self) -> int:
        return (self.on_the_clock_pick - 1) // self.num_teams + 1

    @property
    def on_the_clock_slot(self) -> int:
        return slot_for_pick(self.on_the_clock_pick, self.num_teams)

    @property
    def is_my_turn(self) -> bool:
        return self.on_the_clock_slot == self.my_slot

    @property
    def me(self) -> TeamState:
        return self.teams[self.my_slot]

    def my_remaining_picks(self) -> list[int]:
        cur = self.on_the_clock_pick
        return [
            pick_number(r, self.my_slot, self.num_teams)
            for r in range(1, self.rounds + 1)
            if pick_number(r, self.my_slot, self.num_teams) >= cur
        ]

    def next_pick_after(self, pick_no: int) -> int | None:
        later = [p for p in self.my_remaining_picks() if p > pick_no]
        return later[0] if later else None

    def picks_between(self, pick_no: int) -> int:
        """Opponent picks between `pick_no` and our following selection."""
        nxt = self.next_pick_after(pick_no)
        return (nxt - pick_no - 1) if nxt else 0

    def is_available(self, player_id: str) -> bool:
        return player_id not in self.drafted

    # -- opponent modelling ----------------------------------------------
    def upcoming_slots(self, pick_no: int, count: int) -> list[int]:
        return [
            slot_for_pick(p, self.num_teams)
            for p in range(pick_no, min(pick_no + count, self.rounds * self.num_teams + 1))
        ]

    def positional_demand(self, pick_no: int, count: int) -> dict[str, float]:
        """Summed need across the teams picking in the next `count` selections.

        This is what turns 'RB is scarce' into an actual number: if the eight
        teams between our picks collectively need six RBs, RB value is going to
        evaporate before we pick again.
        """
        demand: dict[str, float] = {}
        for slot in self.upcoming_slots(pick_no, count):
            team = self.teams.get(slot)
            if not team:
                continue
            for pos in ("QB", "RB", "WR", "TE", "K", "DEF"):
                demand[pos] = demand.get(pos, 0.0) + team.needs_score(pos)
        return {k: round(v, 2) for k, v in demand.items()}

    def recent_position_run(self, window: int = 6) -> dict[str, int]:
        """Detect a positional run in the last `window` picks -- runs are the
        main reason a player you expected to survive does not."""
        counts: dict[str, int] = {}
        for rp in self.picks[-window:]:
            pos = rp.get("_resolved_position") or (rp.get("metadata") or {}).get("position")
            if pos and pos != "?":
                counts[pos] = counts.get(pos, 0) + 1
        return counts
