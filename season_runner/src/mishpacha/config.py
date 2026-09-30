"""League constants.

WHO we are (league, account, the other managers) is private and lives outside
the code: a small JSON file named by $MISHPACHA_LEAGUE_FILE, or
config/league.json next to the project. The Home Assistant add-on writes that
file from its own settings. Nothing identifying is kept in this module.

The league FORMAT below (teams, roster, scoring) was read from the live Sleeper
API, not assumed; re-verify with `mish sync` if the commissioner changes it.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


def _private() -> dict:
    here = Path(__file__).resolve().parents[2] / "config" / "league.json"
    for candidate in (os.environ.get("MISHPACHA_LEAGUE_FILE"), here):
        if not candidate:
            continue
        try:
            return json.loads(Path(candidate).read_text())
        except (OSError, ValueError):
            continue
    return {}


_P = _private()

# --------------------------------------------------------------------------
# Identity (private -- see the module docstring)
# --------------------------------------------------------------------------
LEAGUE_ID = str(_P.get("league_id") or "")
DRAFT_ID = str(_P.get("draft_id") or "")
LEAGUE_NAME = _P.get("league_name") or "Fantasy League"
SEASON = str(_P.get("season") or "2026")

INVITE_URL = _P.get("invite_url") or ""

MY_USER_ID = str(_P.get("user_id") or "")
MY_USERNAME = _P.get("username") or ""
MY_DRAFT_SLOT = int(_P.get("draft_slot") or 1)

# --------------------------------------------------------------------------
# Format
# --------------------------------------------------------------------------
# Re-verified against Sleeper on 2026-09-03 after the commissioner resized the
# league from 12 teams to 10. That change also moved rounds 15 -> 16 and bench
# 5 -> 6, so it touches replacement levels, every pick number we own, and the
# gap between our picks (12 opponents between picks 1 and 2 became 8).
NUM_TEAMS = 10
DRAFT_ROUNDS = 16
DRAFT_TYPE = "snake"
PICK_TIMER_SECONDS = 120
CPU_AUTOPICK = True  # league has autopick enabled -- this is our safety net

ROSTER_POSITIONS = [
    "QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "K", "DEF",
    "BN", "BN", "BN", "BN", "BN", "BN",
]

# Positions a FLEX slot accepts in this league.
FLEX_ELIGIBLE = frozenset({"RB", "WR", "TE"})

STARTER_SLOTS: dict[str, int] = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DEF": 1}
FLEX_SLOTS = 2
BENCH_SLOTS = 6
IR_SLOTS = 2
ROSTER_SIZE = sum(STARTER_SLOTS.values()) + FLEX_SLOTS + BENCH_SLOTS  # 15

# --------------------------------------------------------------------------
# League operations
# --------------------------------------------------------------------------
FAAB_BUDGET = 100
WAIVER_DAY_OF_WEEK = 2          # Sleeper: 0=Mon .. so 2 == Wednesday
WAIVER_CLEAR_DAYS = 2
TRADE_DEADLINE_WEEK = 11
TRADE_REVIEW_DAYS = 2
VETO_VOTES_NEEDED = 7
PLAYOFF_TEAMS = 6
PLAYOFF_WEEK_START = 15
REGULAR_SEASON_WEEKS = 14       # weeks 1-14, playoffs 15-17

# 2026 Week 1 opens Wednesday 9 September (game dates 9/9, 9/10, 9/13, 9/14),
# read from Sleeper's weekly projection feed rather than assumed. Drafting on
# or after kickoff costs the league a scoring week -- with playoffs starting
# week 15, a 14-game regular season becomes 13.
WEEK1_KICKOFF = date(2026, 9, 9)
LAST_SAFE_DRAFT_DAY = date(2026, 9, 8)
TARGET_DRAFT_WINDOW = (date(2026, 9, 5), date(2026, 9, 6))
MAX_KEEPERS = 1

# --------------------------------------------------------------------------
# Scoring -- verbatim from the league's scoring_settings, trimmed to the
# categories with non-zero weight. Anything absent scores 0.
# --------------------------------------------------------------------------
SCORING: dict[str, float] = {
    # Passing
    "pass_yd": 0.04,        # 1 pt / 25 yards
    "pass_td": 4.0,
    "pass_int": -1.0,
    "pass_2pt": 2.0,
    # Rushing
    "rush_yd": 0.1,
    "rush_td": 6.0,
    "rush_2pt": 2.0,
    # Receiving -- FULL PPR
    "rec": 1.0,
    "rec_yd": 0.1,
    "rec_td": 6.0,
    "rec_2pt": 2.0,
    # Fumbles
    "fum_lost": -2.0,
    "fum_rec_td": 6.0,
    # Kicking
    "xpm": 1.0,
    "xpmiss": -1.0,
    "fgm_0_19": 3.0,
    "fgm_20_29": 3.0,
    "fgm_30_39": 3.0,
    "fgm_40_49": 4.0,
    "fgm_50_59": 5.0,
    "fgm_60p": 6.0,
    # Team defense / special teams
    "sack": 1.0,
    "int": 2.0,
    "fum_rec": 2.0,
    "ff": 1.0,
    "safe": 2.0,
    "blk_kick": 2.0,
    "def_td": 6.0,
    "st_td": 6.0,
    "def_st_td": 6.0,
    "st_fum_rec": 1.0,
    "def_st_fum_rec": 1.0,
    "st_ff": 1.0,
    "def_st_ff": 1.0,
    "pts_allow_0": 10.0,
    "pts_allow_1_6": 7.0,
    "pts_allow_7_13": 4.0,
    "pts_allow_14_20": 1.0,
    "pts_allow_21_27": 0.0,
    "pts_allow_28_34": -1.0,
    "pts_allow_35p": -4.0,
}

# Notable ABSENCES that shape strategy (verified zero in the live settings):
#   - No TE premium (bonus_rec_te == 0)
#   - No first-down bonuses (bonus_fd_* == 0)
#   - No yardage bonuses (bonus_rush_yd_100, bonus_rec_yd_100 == 0)
#   - No FG-miss penalty beyond XP (fgmiss == 0)
#   - No yards-allowed component for DEF -- points-allowed only
#   - No IDP
SCORING_NOTES = {
    "te_premium": False,
    "first_down_bonus": False,
    "yardage_bonus": False,
    "fg_miss_penalty": False,
    "def_yards_allowed": False,
    "idp": False,
    "ppr": 1.0,
}


# --------------------------------------------------------------------------
# Snake draft math
# --------------------------------------------------------------------------
def pick_number(round_: int, slot: int, num_teams: int = NUM_TEAMS) -> int:
    """Overall pick number for a (1-indexed) round and draft slot in a snake."""
    if round_ % 2 == 1:
        return (round_ - 1) * num_teams + slot
    return (round_ - 1) * num_teams + (num_teams - slot + 1)


def my_picks(slot: int = MY_DRAFT_SLOT, rounds: int = DRAFT_ROUNDS) -> list[int]:
    """Every overall pick number we own."""
    return [pick_number(r, slot) for r in range(1, rounds + 1)]


def slot_for_pick(pick_no: int, num_teams: int = NUM_TEAMS) -> int:
    """Inverse of pick_number: which draft slot owns this overall pick."""
    idx = (pick_no - 1) % num_teams
    round_ = (pick_no - 1) // num_teams + 1
    return idx + 1 if round_ % 2 == 1 else num_teams - idx


def picks_until_next(current_pick: int, slot: int = MY_DRAFT_SLOT) -> int | None:
    """How many other teams pick between now and our next selection."""
    upcoming = [p for p in my_picks(slot) if p > current_pick]
    return (upcoming[0] - current_pick - 1) if upcoming else None


# --------------------------------------------------------------------------
# Replacement level -- the single most important league-specific number.
#
# TWO flex slots is the defining quirk of this league. 12 teams x 2 flex = 24
# extra RB/WR/TE starters league-wide on top of the base requirements, which
# pushes replacement level far deeper than a standard 1-flex league and makes
# startable depth the scarce resource.
#
# Flex allocation below is the empirical full-PPR split (WR-tilted because
# receptions are worth a full point and WR usage is more stable week to week).
# `model.replacement` re-derives this from actual projections at runtime;
# these are the priors used before projections are loaded.
# --------------------------------------------------------------------------
FLEX_ALLOCATION_PRIOR = {"WR": 0.57, "RB": 0.39, "TE": 0.04}


def replacement_ranks(
    num_teams: int = NUM_TEAMS,
    flex_slots: int = FLEX_SLOTS,
    allocation: dict[str, float] | None = None,
) -> dict[str, int]:
    """Positional rank that defines replacement level for each position.

    A player's value is measured against the *worst starter* at their position
    once flex demand is accounted for. Returns e.g. {"RB": 33, "WR": 38, ...}
    meaning RB33 and WR38 are the replacement-level baselines.
    """
    allocation = allocation or FLEX_ALLOCATION_PRIOR
    total_flex = num_teams * flex_slots

    ranks: dict[str, int] = {}
    for pos, base in STARTER_SLOTS.items():
        starters = base * num_teams
        if pos in FLEX_ELIGIBLE:
            starters += round(total_flex * allocation.get(pos, 0.0))
        ranks[pos] = starters
    return ranks


# Precomputed for reference/tests. With 2 FLEX in a 12-team league:
#   QB 12 | RB 24+9=33 | WR 24+14=38 | TE 12+1=13 | K 12 | DEF 12
REPLACEMENT_RANKS = replacement_ranks()

# Target roster shape at the end of 16 rounds. Six bench spots for 10 starting
# slots is still tight, but the extra seat over the old 12-team/15-round setup
# buys exactly one speculative flier.
TARGET_ROSTER_SHAPE = {"QB": 1, "RB": 6, "WR": 6, "TE": 1, "K": 1, "DEF": 1}
MIN_ROSTER_SHAPE = {"QB": 1, "RB": 4, "WR": 5, "TE": 1, "K": 1, "DEF": 1}
MAX_ROSTER_SHAPE = {"QB": 2, "RB": 8, "WR": 9, "TE": 2, "K": 1, "DEF": 2}


# --------------------------------------------------------------------------
# Opponents -- draft order posted by the commissioner on 2026-08-23.
# sleeper_user_id is filled where we could match a display_name with
# confidence; the rest resolve at sync time once everyone joins.
# --------------------------------------------------------------------------
@dataclass
class Manager:
    slot: int
    label: str
    sleeper_user_id: str | None = None
    display_name: str | None = None
    roster_id: int | None = None
    notes: str = ""
    # Behavioral priors learned from live draft observation.
    tendencies: dict = field(default_factory=dict)


# The draft order, one entry per manager, from the private league file.
DRAFT_ORDER: list[Manager] = [Manager(**m) for m in _P.get("managers", [])]

# Joined but not yet matched to a name in the posted order.
UNMATCHED_USERS: dict = dict(_P.get("unmatched_users") or {})
