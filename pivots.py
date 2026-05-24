"""
Floor-trader pivot + support/resistance levels. Pure, no I/O.

These are the 9 anchors the NEW pivot/SuperTrend strategy trades off
(`pivot_st_strategy.py`). Computed from a source window's H/L/C - same source
window as the daily CPR (prior trading FX day), so the runtime feeds the same
HLC it already caches.

Formulas (from the "CE" reference indicator):

    P  = (H + L + C) / 3
    range = H - L

    R1 = 2P - L      S1 = 2P - H
    R2 = P + range   S2 = P - range
    R3 = R1 + range  S3 = S1 - range
    R4 = R2 + range  S4 = S2 - range

Note this R4/S4 convention is R4 = P + 2*range, S4 = P - 2*range (NOT the
"R3 + range" extension). TC/BC are deliberately NOT part of this module - the
new strategy ignores them after asset selection.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Pivots:
    """The 9 daily anchors. Ordered high->low when listed via as_levels()."""
    high: float
    low: float
    close: float
    pp: float
    r1: float
    r2: float
    r3: float
    r4: float
    s1: float
    s2: float
    s3: float
    s4: float

    def as_levels(self) -> list[tuple[str, float]]:
        """All 9 (name, price) pairs, ordered top (R4) to bottom (S4)."""
        return [
            ("R4", self.r4),
            ("R3", self.r3),
            ("R2", self.r2),
            ("R1", self.r1),
            ("P", self.pp),
            ("S1", self.s1),
            ("S2", self.s2),
            ("S3", self.s3),
            ("S4", self.s4),
        ]


def compute_pivots(high: float, low: float, close: float) -> Pivots:
    """Compute the 9 levels from a source window's H, L, C."""
    if high < low:
        raise ValueError(f"high ({high}) < low ({low})")
    if high <= 0 or low <= 0 or close <= 0:
        raise ValueError("HLC values must be positive")

    pp = (high + low + close) / 3.0
    rng = high - low

    r1 = 2.0 * pp - low
    r2 = pp + rng
    r3 = r1 + rng
    r4 = r2 + rng

    s1 = 2.0 * pp - high
    s2 = pp - rng
    s3 = s1 - rng
    s4 = s2 - rng

    return Pivots(
        high=high, low=low, close=close, pp=pp,
        r1=r1, r2=r2, r3=r3, r4=r4,
        s1=s1, s2=s2, s3=s3, s4=s4,
    )
