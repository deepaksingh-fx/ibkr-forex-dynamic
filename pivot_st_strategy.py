"""
Pivot/SR + Quality-SuperTrend strategy (single pair, per-bar).

This is the NEW strategy. It is independent of the old `cpr_st_strategy.py`
(which stays untouched). It reuses the daily-narrowest asset selection and the
CPR source window, but after selection it trades off the 9 pivot/SR anchors
(`pivots.Pivots`) and the quality-filtered SuperTrend (`quality_supertrend`).
TC/BC are NOT used here.

RULES
-----
Base level (the day's anchor; DYNAMIC):
  - First 5-min candle of the FX day SEEDS the base and is NOT eligible for
    entry. Seeding: touched = {level L : low <= L <= high}.
      * 1 touched   -> that level
      * 2+ touched  -> level closest to close, among the touched
      * 0 touched   -> level closest to close, among all 9 (seed fallback only)
  - Every later candle: if it touches any level(s), the base UPDATES by the
    same 1-touch / closest-among-touched rule. If it touches nothing, the base
    is unchanged.

Direction gate (per bar, vs the CURRENT base):
  close > base -> long bias ; close < base -> short bias ; == -> none

Entry (only when flat, only from candle #2 onward, never on the force-exit bar):
  Gate 1: bias from close vs base
  Gate 2: SuperTrend colour confirms - GREEN for long, RED for short
  Both align -> enter on that candle's close.

Exit (single rule, plus EOD):
  - SuperTrend direction FLIP against the position closes it, regardless of the
    resulting colour (a flip into GREY still exits). Same-side GREY does NOT
    exit - it only blocks new entries.
  - Force-exit: at the bar that CLOSES at force_exit_close_time (~16:55 NY),
    any open position is closed unconditionally; no entry on that bar.

Bias change never closes a position. When the base moves and the bias flips
while we hold a position, the new bias governs the NEXT trade only; the open
position is held until a SuperTrend flip.

Same-bar reversal: when a flip exit fires, the opposite-side entry gates are
evaluated on the SAME bar; if they align, the reverse position opens at once.

One position at a time; multiple round-trips per day are allowed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Optional

from pivots import Pivots
from quality_supertrend import QSTConfig, QualitySuperTrend


@dataclass(frozen=True)
class PivotEvent:
    """One trading decision on one bar (0, 1, or 2 per bar)."""
    timestamp: datetime
    action: str             # ENTRY_LONG | ENTRY_SHORT | EXIT_FLIP | EXIT_EOD | REVERSE_TO_LONG | REVERSE_TO_SHORT
    price: float
    reason: str             # GATES_ALIGN | SUPERTREND_FLIP | FORCE_EXIT_EOD
    bias: str               # LONG | SHORT | NONE
    base_level_name: str
    base_level: float
    st_state: str           # GREEN | RED | GREY
    st_dir: int             # +1 up, -1 down, 0 warming
    new_position: int       # state after this event


@dataclass(frozen=True)
class PivotBarOutcome:
    timestamp: datetime
    close: float
    base_level_name: Optional[str]
    base_level: Optional[float]
    bias: str
    st_state: str
    st_dir: int
    flipped_this_bar: bool
    is_first_candle: bool
    position_before: int
    position_after: int
    events: list[PivotEvent]
    # Raw filter values for the bar (for live observability / debugging).
    adx: Optional[float] = None
    ema_fast: Optional[float] = None
    supertrend: Optional[float] = None


class PivotSuperTrendStrategy:
    """Per-bar state machine for the pivot/SR + quality-SuperTrend strategy."""

    def __init__(
        self,
        qst_cfg: Optional[QSTConfig] = None,
        force_exit_close_time: Optional[time] = time(16, 55),
    ):
        self.st = QualitySuperTrend(qst_cfg or QSTConfig())
        self.force_exit_close_time = force_exit_close_time

        self.position: int = 0
        self.entry_price: Optional[float] = None
        self.entry_timestamp: Optional[datetime] = None

        # Flip detection: ST direction of the prior (non-warming) bar.
        self._prev_st_dir: int = 0

        # Dynamic base-level state (reset each FX day).
        self.base_level: Optional[float] = None
        self.base_level_name: Optional[str] = None
        self._current_fx_day_start: Optional[datetime] = None
        self._base_seeded: bool = False     # True once the first candle set a base

    # ------------------------------------------------------------------
    def update(
        self,
        timestamp: datetime,
        open_: float,
        high: float,
        low: float,
        close: float,
        pivots: Pivots,
        fx_day_start: datetime,
    ) -> PivotBarOutcome:
        # 1. Indicator.
        snap = self.st.update(timestamp, open_, high, low, close)
        st_dir = snap.st_dir
        state = snap.state

        # 2. FX-day rollover -> reset base seeding (force-exit already flat us).
        if self._current_fx_day_start is None or fx_day_start != self._current_fx_day_start:
            self._current_fx_day_start = fx_day_start
            self.base_level = None
            self.base_level_name = None
            self._base_seeded = False

        # 3. Base level: seed on the first candle, else update on touch.
        levels = pivots.as_levels()
        touched = [(n, v) for (n, v) in levels if low <= v <= high]
        is_first_candle = not self._base_seeded
        if not self._base_seeded:
            name, val = self._pick(touched if touched else levels, close)
            self.base_level_name, self.base_level = name, val
            self._base_seeded = True
        elif touched:
            name, val = self._pick(touched, close)
            self.base_level_name, self.base_level = name, val
        # else: base unchanged.

        # 4. Flip detection (any change in ST direction between non-warming bars).
        flipped = (self._prev_st_dir != 0 and st_dir != 0 and st_dir != self._prev_st_dir)

        # Force-exit timing: the bar whose CLOSE == force_exit_close_time.
        bar_close_ny = timestamp + timedelta(minutes=5)
        is_force_exit_bar = (
            self.force_exit_close_time is not None
            and bar_close_ny.time() == self.force_exit_close_time
        )

        bias = self._bias(close)
        events: list[PivotEvent] = []
        position_before = self.position

        # 5. EXIT.
        if self.position != 0 and is_force_exit_bar:
            events.append(self._exit_event(
                timestamp, close, "EXIT_EOD", "FORCE_EXIT_EOD", bias, state, st_dir))
            self._close_position()
        elif self.position != 0 and flipped and st_dir != self.position:
            events.append(self._exit_event(
                timestamp, close, "EXIT_FLIP", "SUPERTREND_FLIP", bias, state, st_dir))
            self._close_position()

        # 6. ENTRY (flat, not the seeding candle, not the force-exit bar).
        if (self.position == 0
                and not is_first_candle
                and not is_force_exit_bar
                and self.base_level is not None):
            enter_dir = 0
            if bias == "LONG" and state == "GREEN":
                enter_dir = 1
            elif bias == "SHORT" and state == "RED":
                enter_dir = -1
            if enter_dir != 0:
                is_reversal = any(e.action == "EXIT_FLIP" for e in events)
                if is_reversal:
                    action = "REVERSE_TO_LONG" if enter_dir == 1 else "REVERSE_TO_SHORT"
                else:
                    action = "ENTRY_LONG" if enter_dir == 1 else "ENTRY_SHORT"
                events.append(PivotEvent(
                    timestamp=timestamp, action=action, price=close,
                    reason="GATES_ALIGN", bias=bias,
                    base_level_name=self.base_level_name or "",
                    base_level=self.base_level,
                    st_state=state, st_dir=st_dir, new_position=enter_dir,
                ))
                self._open_position(enter_dir, close, timestamp)

        # 7. Remember ST direction for next bar's flip detection.
        if st_dir != 0:
            self._prev_st_dir = st_dir

        return PivotBarOutcome(
            timestamp=timestamp, close=close,
            base_level_name=self.base_level_name, base_level=self.base_level,
            bias=bias, st_state=state, st_dir=st_dir,
            flipped_this_bar=flipped, is_first_candle=is_first_candle,
            position_before=position_before, position_after=self.position,
            events=events,
            adx=snap.adx, ema_fast=snap.ema_fast, supertrend=snap.supertrend,
        )

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _pick(candidates: list[tuple[str, float]], close: float) -> tuple[str, float]:
        """Level closest to `close`. Tie-break: lower (support-side) level."""
        return min(candidates, key=lambda nv: (abs(nv[1] - close), nv[1]))

    def _bias(self, close: float) -> str:
        if self.base_level is None:
            return "NONE"
        if close > self.base_level:
            return "LONG"
        if close < self.base_level:
            return "SHORT"
        return "NONE"

    def _exit_event(self, ts, price, action, reason, bias, state, st_dir) -> PivotEvent:
        return PivotEvent(
            timestamp=ts, action=action, price=price, reason=reason, bias=bias,
            base_level_name=self.base_level_name or "",
            base_level=self.base_level if self.base_level is not None else 0.0,
            st_state=state, st_dir=st_dir, new_position=0,
        )

    def _open_position(self, side: int, price: float, ts: datetime) -> None:
        self.position = side
        self.entry_price = price
        self.entry_timestamp = ts

    def _close_position(self) -> None:
        self.position = 0
        self.entry_price = None
        self.entry_timestamp = None
