"""Live draft monitor: watch the Sleeper draft and recommend in real time."""
from __future__ import annotations

import time
from dataclasses import dataclass

from . import config as cfg
from .data import sleeper
from .draft.recommend import DraftBrain, Recommendation
from .draft.state import DraftState
from .model.board import BoardEntry


@dataclass
class LiveDraft:
    brain: DraftBrain
    draft_id: str = cfg.DRAFT_ID
    my_slot: int = cfg.MY_DRAFT_SLOT
    poll_seconds: float = 2.0

    def __post_init__(self) -> None:
        self.state = DraftState(my_slot=self.my_slot)
        self.positions = {e.player_id: e.position for e in self.brain.board}
        self.names = {e.player_id: e.name for e in self.brain.board}
        # Pick number we last computed a recommendation for. The board cannot
        # change while we are on the clock (nobody else can pick), so one
        # computation per pick is both sufficient and deterministic -- without
        # this guard the poll loop burned two seconds of simulation every two
        # seconds for identical output.
        self._recommended_for = -1
        self.slot_conflict: tuple[int, int] | None = None

    def resolve_slot(self) -> int | None:
        """Bind our draft slot from Sleeper, cross-checking BOTH mappings.

        Sleeper exposes the ordering twice and they can disagree. On this
        league, `draft_order` (user -> slot) was missing an entry entirely
        while `slot_to_roster_id` was complete, and the two named different
        managers at slot 9 -- a commissioner mid-edit leaves the pair
        inconsistent.

        `slot_to_roster_id` is the authority: it is roster-based, and the
        `draft_slot` field on every incoming pick is keyed the same way. We
        prefer it, fall back to `draft_order`, and surface a disagreement
        rather than silently drafting from the wrong seat -- which would
        poison every survival calculation and every pick.
        """
        by_roster: int | None = None
        by_user: int | None = None
        try:
            rid = sleeper.my_roster_id(cfg.LEAGUE_ID, cfg.MY_USER_ID)
            if rid is not None:
                for slot, r in sleeper.slot_to_roster(self.draft_id, ttl=0).items():
                    if r == rid:
                        by_roster = slot
                        break
        except Exception:
            pass
        try:
            for slot, uid in sleeper.draft_slot_map(self.draft_id).items():
                if uid == cfg.MY_USER_ID:
                    by_user = slot
                    break
        except Exception:
            pass

        if by_roster and by_user and by_roster != by_user:
            self.slot_conflict = (by_roster, by_user)
        return by_roster or by_user

    def sync(self) -> int:
        picks = sleeper.draft_picks(self.draft_id)
        # Sleeper's pick metadata carries position; prefer our board's mapping.
        return self.state.apply_picks(picks, self.positions)

    def poll_once(self) -> tuple[int, Recommendation | None]:
        """Returns (new_picks, recommendation) -- the recommendation is
        produced exactly once per on-the-clock pick."""
        new = self.sync()
        rec = None
        # A gapped feed means Sleeper served a lagging snapshot; recommending
        # against it would simulate with an already-drafted player available.
        # It self-heals on the next poll two seconds later.
        if (self.state.is_synced
                and self.state.is_my_turn
                and self.state.on_the_clock_pick != self._recommended_for):
            rec = self.brain.recommend(self.state)
            self._recommended_for = self.state.on_the_clock_pick
        return new, rec

    def pick_feed(self, limit: int = 10) -> list[str]:
        out = []
        for rp in self.state.picks[-limit:]:
            pid = rp.get("player_id")
            md = rp.get("metadata") or {}
            # Board covers 633 fantasy-relevant players; a deep pick outside it
            # still deserves a name in the feed, and Sleeper sends one.
            nm = (self.names.get(pid)
                  or " ".join(filter(None, [md.get("first_name"), md.get("last_name")]))
                  or pid)
            pos = self.positions.get(pid) or md.get("position") or "?"
            slot = rp.get("draft_slot")
            mgr = next((m.label for m in cfg.DRAFT_ORDER if m.slot == slot), f"slot {slot}")
            no = rp.get("pick_no")
            out.append(f"#{(no if no is not None else '?'):>3}  {mgr:<14} {nm} ({pos})")
        return out
