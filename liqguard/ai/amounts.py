"""
Which dollar amounts a Muse answer may quote.

Muse may only repeat figures from its input, but it writes them the way a
reader would: rounded to the dollar, as "$1.4k", or as the sum of two costs
of the same option. An amount passes when it is within RELATIVE_TOLERANCE
(or ABSOLUTE_TOLERANCE) of an allowed figure; anything else is a number
Muse made up, and the answer is rejected.
"""

from __future__ import annotations

import re
from itertools import combinations
from typing import Iterable, Optional

RELATIVE_TOLERANCE = 0.01
ABSOLUTE_TOLERANCE = 0.5

DOLLARS = re.compile(r"\$\s?([0-9][0-9,]*(?:\.[0-9]+)?)\s*([kKmM](?![a-zA-Z]))?")


def quoted(text: str) -> list[tuple[str, float]]:
    """Every `$` amount in `text`: ("$1.4k", 1400.0)."""
    out = []
    for m in DOLLARS.finditer(text):
        value = float(m.group(1).replace(",", ""))
        suffix = (m.group(2) or "").lower()
        value *= {"k": 1e3, "m": 1e6}.get(suffix, 1)
        out.append((m.group(0).strip(), value))
    return out


def fits(value: float, allowed: Iterable[float]) -> bool:
    for a in allowed:
        if abs(value - a) <= max(ABSOLUTE_TOLERANCE, RELATIVE_TOLERANCE * abs(a)):
            return True
    return False


def with_sums(groups: Iterable[Iterable[Optional[float]]]) -> set[float]:
    """Each figure (and its absolute value), plus pairwise sums within a group (e.g. one option's fee + funding)."""
    out: set[float] = set()
    for group in groups:
        values = [abs(v) for v in group if isinstance(v, (int, float)) and not isinstance(v, bool)]
        out.update(values)
        out.update(a + b for a, b in combinations([v for v in values if v], 2))
    return out


def unlisted(text: str, allowed: set[float]) -> Optional[str]:
    """The first quoted amount that fits no allowed figure, else None."""
    return next((shown for shown, value in quoted(text) if not fits(value, allowed)), None)
