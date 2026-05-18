"""
CPR + Regime + Adaptive SuperTrend strategy (single pair, per-bar).

Three-gate entry, single-rule exit, same-bar reversal.

ENTRY (all three must align on a 5-min candle close, position is FLAT):
  Gate 1 — CPR bias:
      bias = LONG  if close >  TC
             SHORT if close <  BC
             NONE  if BC ≤ close ≤ TC
  Gate 2 — Regime is DIRECTIONAL (UP or DOWN — the direction itself does
           not need to match the bias; either DIRECTIONAL_* regime passes).
           NON_DIRECTIONAL / MEAN_REVERTING / WARMING_UP → blocks entry.
  Gate 3 — Adaptive SuperTrend direction aligns with bias:
              bias LONG   → active_dir ==  1   (green)
              bias SHORT  → active_dir == -1   (red)

EXIT (single rule for now):
  SuperTrend flip against the current position:
      LONG  & active_dir flips  1 → -1  → close LONG
      SHORT & active_dir flips -1 → 1   → close SHORT
  (Chart-visual definition — any bar-over-bar change in active_dir counts,
  including the rare method-switch artefact.)

FORCE EXIT:
  At the close of the bar whose close-time == force_exit_close_time (a per-
  pair value, typically 16:55 NY for normal 24/5 forex), any open position
  is closed unconditionally. No new entries on the force-exit bar.

SAME-BAR REVERSAL:
  When an exit fires (flip-driven only, NOT force-exit), the new-entry
  conditions for the OPPOSITE direction are evaluated on the SAME bar. If
  all three gates align for the opposite side, a new opposite-direction
  position is opened immediately.

POSITION RULES:
  One position at a time. While long, ignore further long-entry conditions
  (and vice versa). Bias only gates new entries — held positions are not
  re-evaluated against bias.

The classifier is stateful. Feed bars chronologically via .update(...).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from enum import Enum
from typing import Optional

from adaptive_supertrend import (
    METHOD_NAMES,
    AdaptiveSnapshot,
    AdaptiveSTConfig,
    AdaptiveSuperTrend,
)
from regime import Regime, RegimeClassifier, RegimeConfig, RegimeSnapshot


class Position(int, Enum):
    FLAT = 0
    LONG = 1
    SHORT = -1


@dataclass(frozen=True)
class StrategyEvent:
    """One trading decision on one bar. There may be 0, 1, or 2 events per bar
    (2 on a same-bar reversal: EXIT then ENTRY)."""
    timestamp: datetime
    action: str             # ENTRY_LONG | ENTRY_SHORT | EXIT_FLIP | EXIT_EOD | REVERSE_TO_LONG | REVERSE_TO_SHORT
    price: float
    reason: str             # ALL_GATES_ALIGN | SUPERTREND_FLIP | FORCE_EXIT_EOD
    bias: str               # LONG | SHORT | NONE
    regime: str
    regime_directional: bool
    active_dir: int
    active_method: str
    new_position: int       # state after this event (1, -1, 0)


@dataclass(frozen=True)
class BarOutcome:
    """Full snapshot of what happened on this bar."""
    timestamp: datetime
    close: float
    bias: str               # LONG | SHORT | NONE
    regime: str
    regime_directional: bool
    active_dir: int
    active_method: str
    flipped_this_bar: bool   # True iff active_dir != prev_active_dir (post-warmup)
    position_before: int
    position_after: int
    events: list[StrategyEvent]


def _bias_from_close(close: float, tc: float, bc: float) -> str:
    if close > tc:
        return "LONG"
    if close < bc:
        return "SHORT"
    return "NONE"


def _bias_to_int(bias: str) -> int:
    if bias == "LONG":
        return 1
    if bias == "SHORT":
        return -1
    return 0


class CPRSuperTrendStrategy:
    """
    Per-bar strategy state machine.

    Args:
      regime_cfg:               passed to RegimeClassifier
      ast_cfg:                  passed to AdaptiveSuperTrend
      force_exit_close_time:    time-of-day (NY) at which open positions are
                                force-closed on the bar that CLOSES at this
                                moment. e.g. time(16, 55) for normal 24/5
                                forex. If None, no force-exit is applied.
      ast_bars_per_day:         passed to AdaptiveSuperTrend (default 288)
    """

    def __init__(
        self,
        regime_cfg: Optional[RegimeConfig] = None,
        ast_cfg: Optional[AdaptiveSTConfig] = None,
        force_exit_close_time: Optional[time] = time(16, 55),
        ast_bars_per_day: int = 288,
    ):
        self.regime = RegimeClassifier(regime_cfg or RegimeConfig())
        self.ast = AdaptiveSuperTrend(ast_cfg or AdaptiveSTConfig(), bars_per_day=ast_bars_per_day)
        self.force_exit_close_time = force_exit_close_time

        self.position: int = 0
        self.entry_price: Optional[float] = None
        self.entry_timestamp: Optional[datetime] = None

        # Active-dir from the prior bar, for flip detection. Stays 0 until
        # the first bar where active_dir is non-zero.
        self._prev_active_dir: int = 0

    # ──────────────────────────────────────────────────────────────────
    def update(
        self,
        timestamp: datetime,
        open_: float,
        high: float,
        low: float,
        close: float,
        daily_tc: float,
        daily_bc: float,
        daily_pp: float,
        fx_day_start: datetime,
    ) -> BarOutcome:
        """
        Process one closed 5-min bar.

        Returns a BarOutcome with any events generated (0, 1, or 2 events).
        """
        # 1. Update indicators.
        r_snap = self.regime.update(timestamp, close, daily_pp, fx_day_start)
        a_snap = self.ast.update(timestamp, open_, high, low, close)

        regime_directional = r_snap.regime in (
            Regime.DIRECTIONAL_UP, Regime.DIRECTIONAL_DOWN
        )
        bias = _bias_from_close(close, daily_tc, daily_bc)
        bias_int = _bias_to_int(bias)
        active_dir = a_snap.active_dir
        active_method = METHOD_NAMES[a_snap.active_method_idx]

        # Flip detection (chart-visual).
        flipped = (
            self._prev_active_dir != 0
            and active_dir != 0
            and active_dir != self._prev_active_dir
        )

        # Force-exit timing: the bar that CLOSES at force_exit_close_time
        # has open_time = force_exit_close_time - 5 minutes. Compute the
        # bar's CLOSE time and compare.
        bar_close_ny = timestamp + timedelta(minutes=5)
        is_force_exit_bar = (
            self.force_exit_close_time is not None
            and bar_close_ny.time() == self.force_exit_close_time
        )

        events: list[StrategyEvent] = []
        position_before = self.position

        # ─── EXIT logic ───
        # 1a. Force-exit at EOD (highest priority).
        if self.position != 0 and is_force_exit_bar:
            events.append(self._exit_event(
                timestamp, close, "EXIT_EOD", "FORCE_EXIT_EOD",
                bias, r_snap, regime_directional, active_dir, active_method,
            ))
            self._close_position()

        # 1b. SuperTrend-flip exit.
        elif self.position != 0 and flipped and active_dir != self.position:
            events.append(self._exit_event(
                timestamp, close, "EXIT_FLIP", "SUPERTREND_FLIP",
                bias, r_snap, regime_directional, active_dir, active_method,
            ))
            self._close_position()
            # Same-bar reversal evaluation happens below in the ENTRY block.

        # ─── ENTRY logic (only when flat) ───
        # No new entries on the force-exit bar — even if all three gates would
        # otherwise align (including the same-bar reversal case after a flip).
        if self.position == 0 and not is_force_exit_bar:
            if (bias_int != 0
                    and regime_directional
                    and active_dir == bias_int):
                # Is this a same-bar reversal? (Only true if we just exited via
                # a flip in this same .update() call.)
                is_reversal = any(e.action == "EXIT_FLIP" for e in events)
                action = (
                    ("REVERSE_TO_LONG" if bias_int == 1 else "REVERSE_TO_SHORT")
                    if is_reversal else
                    ("ENTRY_LONG" if bias_int == 1 else "ENTRY_SHORT")
                )
                events.append(StrategyEvent(
                    timestamp=timestamp,
                    action=action,
                    price=close,
                    reason="ALL_GATES_ALIGN",
                    bias=bias,
                    regime=r_snap.regime.value,
                    regime_directional=regime_directional,
                    active_dir=active_dir,
                    active_method=active_method,
                    new_position=bias_int,
                ))
                self._open_position(bias_int, close, timestamp)

        # Update prev_active_dir for next bar's flip detection.
        if active_dir != 0:
            self._prev_active_dir = active_dir

        return BarOutcome(
            timestamp=timestamp,
            close=close,
            bias=bias,
            regime=r_snap.regime.value,
            regime_directional=regime_directional,
            active_dir=active_dir,
            active_method=active_method,
            flipped_this_bar=flipped,
            position_before=position_before,
            position_after=self.position,
            events=events,
        )

    # ─── helpers ───
    def _exit_event(
        self, ts, price, action, reason, bias, r_snap, reg_dir,
        active_dir, active_method,
    ) -> StrategyEvent:
        return StrategyEvent(
            timestamp=ts, action=action, price=price, reason=reason,
            bias=bias, regime=r_snap.regime.value,
            regime_directional=reg_dir,
            active_dir=active_dir, active_method=active_method,
            new_position=0,
        )

    def _open_position(self, side: int, price: float, ts: datetime) -> None:
        self.position = side
        self.entry_price = price
        self.entry_timestamp = ts

    def _close_position(self) -> None:
        self.position = 0
        self.entry_price = None
        self.entry_timestamp = None
