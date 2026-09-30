"""Will this player still be there at our next pick?

Everything strategic in a snake draft reduces to this question. With 12 picks
between our first and second selection, the cost of taking a player early is
only real if someone else would have taken them; the cost of waiting is only
real if they will not survive.

Model: a per-pick hazard derived from consensus ADP, modulated by the actual
positional need of the specific teams picking in between, plus a run-detection
term. Survival is the product of (1 - hazard) across intervening picks.
"""
from __future__ import annotations

import math

from .state import DraftState

SQRT2 = math.sqrt(2.0)


def _phi(z: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(z / SQRT2))


# Fitted by least squares through the origin against the 232 players in this
# league's own board that carry a MEASURED FantasyFootballCalculator stdev.
# The previous coefficient (0.28*adp + 1.5, floor 3.0) ran 2.2x-2.9x too wide
# in every ADP bucket, and 47% of draftable players fall back to this formula
# -- so it was not a display detail: it fed the Monte Carlo's draft-order noise
# and simulated half the board as far more random than drafters actually are.
ADP_SD_SLOPE = 0.111
ADP_SD_FLOOR = 1.0


def default_adp_sd(adp: float) -> float:
    """Dispersion of ADP, for players with no measured stdev.

    Scales linearly with ADP: the top few picks are near-deterministic while
    round-12 opinions scatter badly. Prefer a real measured stdev wherever the
    ADP feed supplies one; this is the fallback.
    """
    return max(ADP_SD_FLOOR, ADP_SD_SLOPE * adp)


def base_availability(adp: float, pick_no: int, adp_sd: float | None = None) -> float:
    """P(player is still on the board when pick `pick_no` arrives), from ADP alone."""
    sd = adp_sd if adp_sd and adp_sd > 0 else default_adp_sd(adp)
    # P(taken before this pick) = Phi((pick - adp)/sd)
    return max(0.0, min(1.0, 1.0 - _phi((pick_no - adp) / sd)))


# NOTE: `survival_probability` and `survives_to_next` used to live here. They
# were an analytic per-pick hazard model, and they were wrong in a way worth
# recording so nobody rebuilds them.
#
# Exactly one player comes off the board per pick, so summing P(taken) across
# every available player over a single opponent pick must equal 1.0. Measured
# on this league's board it summed to 1.89 from the ADP marginals alone and
# 2.68 once the roster-need multiplier was applied -- the need term
# double-counted scarcity the ADP prior already encodes, and in the opening
# rounds every team needs every position, so the advertised "per-team
# modulation" collapsed to a single constant. Against the simulator the model
# understated availability by up to 12x in round one and overstated it by ~3x
# by round seven, with the bias reversing sign mid-draft.
#
# Survival is now measured by `simulate.empirical_survival`, which counts
# outcomes from the same opponent model that chooses the pick. One model
# cannot disagree with itself.
