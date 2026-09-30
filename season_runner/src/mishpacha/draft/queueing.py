"""Pre-draft queue construction.

A Sleeper queue is consumed top-down as players come off the board, so it must
be ordered the way we would actually pick -- not merely sorted by value. It is
the draft-day dead-man's-switch: autopick follows the queue before Sleeper's
own default ranking, and the default ranking is unusable (a search-popularity
index carrying retired players inside its top 100).
"""
from __future__ import annotations

# Looser than roster caps, because the queue is eaten from the top as OTHER
# teams draft -- it needs depth at RB/WR to survive being consumed unattended.
QUEUE_CAPS = {"QB": 3, "TE": 4, "RB": 99, "WR": 99}


def build_queue(board, depth: int = 40, adp_limit: float = 190.0) -> list:
    """Return BoardEntries in queue order.

    Two corrections to a raw VOR sort:
      * Kickers and defenses go LAST regardless of their VOR. A defense can
        rank 20th by value, but a queue that lists one there would have the
        autopicker taking a defense in round two.
      * Positional caps stop the queue front-loading three quarterbacks.
    """
    counts: dict[str, int] = {}
    skill = []
    for e in sorted(board, key=lambda e: -e.vor):
        if e.adp > adp_limit or e.position in ("K", "DEF"):
            continue
        if counts.get(e.position, 0) >= QUEUE_CAPS.get(e.position, 99):
            continue
        counts[e.position] = counts.get(e.position, 0) + 1
        skill.append(e)
        if len(skill) >= max(depth - 2, 1):
            break

    by_vor = sorted(board, key=lambda x: -x.vor)
    tail = [next((e for e in by_vor if e.position == pos), None) for pos in ("DEF", "K")]
    return skill + [e for e in tail if e]
