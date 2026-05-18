"""
Market-regime classifier — Python port of the Pine v6 "Regime Decider".

Classifies each 5-min bar into one of:
  - DIRECTIONAL_UP / DIRECTIONAL_DOWN
  - MEAN_REVERTING
  - NON_DIRECTIONAL
  - WARMING_UP   (insufficient history)

Methodology (matches the Pine indicator):

  Metric 1: Efficiency Ratio (Kaufman) over last `er_len` bars
    ER = |close - close[er_len]| / Σ |close[i] - close[i-1]|
    Bounded [0, 1]. 1 = pure trend, 0 = pure noise.

  Metric 2: Close-to-close crossings of the daily PP over last `cross_len`
    bars. The bar that rolls over to a new FX day is excluded (the PP itself
    just changed value, which would create a spurious cross).

Classification (priority, mutually exclusive):
  1. ER hysteresis says directional → DIRECTIONAL_UP / DIRECTIONAL_DOWN
  2. else crossings ≥ threshold → MEAN_REVERTING
  3. else → NON_DIRECTIONAL

ER hysteresis: sticky. Enter directional when ER > er_enter; exit only when
ER < er_exit. Prevents flicker near the threshold.

The "daily PP" is the central pivot of the PRIOR FX day's HLC
(same Pivot as `cpr.CPR.pivot`). Callers supply the per-bar PP value;
the classifier tracks state across bars.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class Regime(str, Enum):
    DIRECTIONAL_UP = "DIRECTIONAL_UP"
    DIRECTIONAL_DOWN = "DIRECTIONAL_DOWN"
    MEAN_REVERTING = "MEAN_REVERTING"
    NON_DIRECTIONAL = "NON_DIRECTIONAL"
    WARMING_UP = "WARMING_UP"


@dataclass(frozen=True)
class RegimeConfig:
    er_len: int = 20            # ER lookback (bars). Pine default.
    er_enter: float = 0.40      # Enter directional when ER >  this.
    er_exit: float = 0.30       # Exit directional when ER  <  this. Hysteresis.
    cross_len: int = 30         # PP-crossings rolling window (bars).
    cross_threshold: int = 3    # Min crossings for MEAN_REVERTING.

    def __post_init__(self):
        if self.er_len < 2:
            raise ValueError("er_len must be >= 2")
        if not (0.0 < self.er_exit < self.er_enter <= 1.0):
            raise ValueError("0 < er_exit < er_enter <= 1.0 required (hysteresis)")
        if self.cross_len < 2:
            raise ValueError("cross_len must be >= 2")
        if self.cross_threshold < 1:
            raise ValueError("cross_threshold must be >= 1")


@dataclass(frozen=True)
class RegimeSnapshot:
    timestamp: datetime
    close: float
    daily_pp: float
    er: Optional[float]         # None during warm-up
    crossings: int
    in_directional: bool
    regime: Regime
    fx_day_start: datetime


class RegimeClassifier:
    """
    Stateful per-bar classifier. Feed bars chronologically.

    Args to `.update`:
      timestamp:    bar OPEN time, tz-aware (any tz; classifier doesn't care)
      close:        bar close price
      daily_pp:     pivot from PRIOR FX day's HLC, constant within an FX day
      fx_day_start: NY-anchored 17:00 start of THIS bar's FX day; used to
                    detect the rollover bar (suppress its spurious "cross")
    """

    def __init__(self, config: RegimeConfig = RegimeConfig()):
        self.config = config
        # Closes ring: holds up to er_len + 1 (so closes[0] is close[er_len]
        # once full, the reference for net_change in the ER formula).
        self._closes: deque[float] = deque(maxlen=config.er_len + 1)
        # Per-bar |Δclose| for the ER denominator.
        self._bar_changes: deque[float] = deque(maxlen=config.er_len)
        # Rolling crossing flags (1 = crossed PP this bar, 0 = didn't).
        self._crosses: deque[int] = deque(maxlen=config.cross_len)
        # Previous-bar state for cross detection.
        self._prev_close: Optional[float] = None
        self._prev_pp: Optional[float] = None
        self._prev_fx_day_start: Optional[datetime] = None
        # Hysteresis state.
        self._in_directional: bool = False

    def update(
        self,
        timestamp: datetime,
        close: float,
        daily_pp: float,
        fx_day_start: datetime,
    ) -> RegimeSnapshot:
        cfg = self.config

        # Rollover-bar guard: matches Pine's `not newDay`.
        is_new_day = (
            self._prev_fx_day_start is not None
            and fx_day_start != self._prev_fx_day_start
        )

        if self._prev_close is not None:
            self._bar_changes.append(abs(close - self._prev_close))

        # Cross detection on close-to-close, with same-day PP.
        if (self._prev_close is not None
                and self._prev_pp is not None
                and not is_new_day):
            prev_diff = self._prev_close - self._prev_pp
            cur_diff = close - daily_pp
            crossed = (prev_diff > 0 > cur_diff) or (prev_diff < 0 < cur_diff)
        else:
            crossed = False
        self._crosses.append(1 if crossed else 0)

        # Push current close AFTER computing Δ (so it's bar t, not bar t+1).
        self._closes.append(close)

        # ER: needs er_len Δ-samples AND closes[0] anchored er_len bars back.
        er: Optional[float] = None
        if (len(self._bar_changes) == cfg.er_len
                and len(self._closes) == cfg.er_len + 1):
            total = sum(self._bar_changes)
            net = abs(close - self._closes[0])
            er = (net / total) if total > 0 else 0.0

        crossings = sum(self._crosses)

        # Hysteresis state machine — only updates when ER is computable.
        if er is not None:
            if not self._in_directional and er > cfg.er_enter:
                self._in_directional = True
            elif self._in_directional and er < cfg.er_exit:
                self._in_directional = False

        # Classification.
        if er is None:
            regime = Regime.WARMING_UP
        elif self._in_directional:
            ref = self._closes[0]   # = close[er_len]
            regime = Regime.DIRECTIONAL_UP if close > ref else Regime.DIRECTIONAL_DOWN
        elif crossings >= cfg.cross_threshold:
            regime = Regime.MEAN_REVERTING
        else:
            regime = Regime.NON_DIRECTIONAL

        # Save bar-t state for bar-(t+1).
        self._prev_close = close
        self._prev_pp = daily_pp
        self._prev_fx_day_start = fx_day_start

        return RegimeSnapshot(
            timestamp=timestamp,
            close=close,
            daily_pp=daily_pp,
            er=er,
            crossings=crossings,
            in_directional=self._in_directional,
            regime=regime,
            fx_day_start=fx_day_start,
        )
