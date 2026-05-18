"""
CPR computation. Pure, no I/O.

Standard formulas (SPEC sec5):
    Pivot  = (H + L + C) / 3
    BC     = (H + L) / 2
    TC_raw = 2*Pivot - BC

Then enforce TC >= BC by swapping if needed.

Width metric (SPEC sec9.2):
    width_pct = (TC - BC) / Pivot x 100      # standard CPR-literature denominator
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True, slots=True)
class CPR:
    high: float
    low: float
    close: float
    pivot: float
    bc: float
    tc: float

    @property
    def width(self) -> float:
        return self.tc - self.bc

    @property
    def width_pct(self) -> float:
        # Pivot is always > 0 for forex (no zero-priced pairs).
        return (self.width / self.pivot) * 100.0


def compute_cpr_from_hlc(high: float, low: float, close: float) -> CPR:
    """Compute CPR from a window's H, L, C. Force TC >= BC."""
    if high < low:
        raise ValueError(f"high ({high}) < low ({low})")
    if close <= 0 or high <= 0 or low <= 0:
        raise ValueError("HLC values must be positive")

    pivot = (high + low + close) / 3.0
    bc = (high + low) / 2.0
    tc_raw = 2.0 * pivot - bc

    # Force ordering: TC always the upper line.
    tc = max(tc_raw, bc)
    bc_final = min(tc_raw, bc)

    return CPR(high=high, low=low, close=close, pivot=pivot, bc=bc_final, tc=tc)


def compute_cpr_from_bars(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
) -> CPR:
    """
    Compute CPR from a sequence of 5-min bars representing the source window.

    H = max of highs
    L = min of lows
    C = the LAST bar's close (i.e. closes[-1])
    """
    if not highs or not lows or not closes:
        raise ValueError("Empty bar sequence - cannot compute CPR")
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("highs/lows/closes lengths must match")
    H = max(highs)
    L = min(lows)
    C = closes[-1]
    return compute_cpr_from_hlc(H, L, C)
