"""
Bias + DMI signal logic for the coil/session strategy. Pure, no I/O, no orders.

This is the SuperTrend-free core (§3-§5 of SPEC_COIL.md):
  - a DYNAMIC base level chosen from the 9 daily pivots (seed on the session's
    first candle, hop to the nearest touched pivot thereafter);
  - bias = close vs base (LONG-only above, SHORT-only below);
  - entry/exit signal = bias AND DMI (DI+/DI-) must agree.

It deliberately owns ONLY the level/bias/signal state. Position, stops, PnL and
the daily limit are driven by the caller (the backtest or, later, the live
runtime), because those depend on fills the signal layer never sees.
"""
from __future__ import annotations

from typing import Optional, Tuple

from pivots import Pivots


def _pick(candidates: list[tuple[str, float]], close: float) -> tuple[str, float]:
    """Level closest to `close`. Tie-break: the lower (support-side) level."""
    return min(candidates, key=lambda nv: (abs(nv[1] - close), nv[1]))


class CoilBase:
    """Dynamic base level over the 9 pivots. Feed bars chronologically."""

    def __init__(self) -> None:
        self.name: Optional[str] = None
        self.level: Optional[float] = None
        self._seeded: bool = False

    def update(self, high: float, low: float, close: float, pivots: Pivots) -> bool:
        """
        Advance the base for one bar. Returns True iff this was the seeding
        (first) candle. Seed candle sets the base but is NOT entry-eligible.
        """
        levels = pivots.as_levels()
        touched = [(n, v) for (n, v) in levels if low <= v <= high]
        if not self._seeded:
            self.name, self.level = _pick(touched if touched else levels, close)
            self._seeded = True
            return True
        if touched:
            self.name, self.level = _pick(touched, close)
        return False

    def bias(self, close: float) -> str:
        if self.level is None:
            return "NONE"
        if close > self.level:
            return "LONG"
        if close < self.level:
            return "SHORT"
        return "NONE"


def entry_dir(bias: str, plus_di: float, minus_di: float) -> int:
    """
    Desired side when FLAT: +1 long / -1 short / 0 none. Both gates must agree.
      LONG  bias and DI+ > DI-  -> +1
      SHORT bias and DI- > DI+  -> -1
    """
    if bias == "LONG" and plus_di > minus_di:
        return 1
    if bias == "SHORT" and minus_di > plus_di:
        return -1
    return 0


def reverse_signal(position: int, bias: str, plus_di: float, minus_di: float) -> bool:
    """
    True iff the FULL opposite entry signal fires against an open `position`.
    A bias change alone is not enough - DMI must confirm the opposite side too.
    """
    if position > 0:      # long -> need SHORT bias AND DI- > DI+
        return entry_dir(bias, plus_di, minus_di) == -1
    if position < 0:      # short -> need LONG bias AND DI+ > DI-
        return entry_dir(bias, plus_di, minus_di) == 1
    return False
