"""
Quality-Filtered SuperTrend - Python port of the Pine v6 "ST - Quality Filtered"
indicator. Bar-by-bar stateful; feed closed 5-min bars chronologically.

It is the ONLY indicator the new pivot strategy uses (the old regime classifier
and Adaptive SuperTrend are not involved). Output per bar:

  - supertrend: the trailing-stop line value (or None during warm-up)
  - st_dir:     +1 = uptrend, -1 = downtrend, 0 = warming up
  - state:      "GREEN" | "RED" | "GREY"

State logic (matches the Pine indicator, loose defaults):

  isUp        = SuperTrend direction is up
  adxOK       = ADX(14) >= adx_thresh (18)
  emaLongOK   = close > EMA(50)            (stack mode adds ema50 > ema200)
  emaShortOK  = close < EMA(50)            (stack mode adds ema50 < ema200)
  volOK       = ATR(10) > SMA(ATR(10), 50) * vol_mult (0.8)

  GREEN (qualityLong)  = isUp        and adxOK and emaLongOK  and volOK
  RED   (qualityShort) = (not isUp)  and adxOK and emaShortOK and volOK
  GREY  (choppy)       = everything else  -> stand aside (blocks ENTRY only)

The underlying SuperTrend is classic `ta.supertrend(factor, atrPeriod)`:
hl2 source, Wilder ATR. direction < 0 in Pine means uptrend; we expose that as
st_dir = +1. A FLIP is any change in st_dir between consecutive (non-warming)
bars; the strategy exits on a flip regardless of the resulting colour.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

from indicators import EMA, SMA, TrueRange, WilderATR, WilderSmoothing


@dataclass(frozen=True)
class QSTConfig:
    atr_period: int = 10
    factor: float = 2.0
    adx_len: int = 14
    adx_thresh: float = 18.0
    ema_len: int = 50
    use_stack: bool = False
    ema_slow_len: int = 200
    vol_avg_len: int = 50
    vol_mult: float = 0.8

    def __post_init__(self) -> None:
        if self.atr_period < 1 or self.adx_len < 1 or self.ema_len < 1:
            raise ValueError("periods must be >= 1")
        if self.factor <= 0:
            raise ValueError("factor must be > 0")


@dataclass(frozen=True)
class QSTSnapshot:
    timestamp: datetime
    supertrend: Optional[float]
    st_dir: int                 # +1 up, -1 down, 0 warming
    state: str                  # GREEN | RED | GREY
    adx: Optional[float]
    ema_fast: Optional[float]
    atr: Optional[float]


class SuperTrend:
    """Classic `ta.supertrend(factor, atr_period)`. Wilder ATR, hl2 source.

    Returns (supertrend, direction) where direction is +1 (up) / -1 (down),
    or None while ATR is warming up. (Note: Pine encodes up as -1; we flip the
    sign so +1 == up, which reads naturally for the strategy.)
    """

    def __init__(self, factor: float, atr_period: int):
        self.factor = factor
        self._atr = WilderATR(atr_period)
        self._prev_upper: Optional[float] = None
        self._prev_lower: Optional[float] = None
        self._prev_close: Optional[float] = None
        self._prev_supertrend: Optional[float] = None

    def update(self, high: float, low: float, close: float) -> Optional[Tuple[float, int]]:
        atr = self._atr.update(high, low, close)
        if atr is None:
            self._prev_close = close
            return None

        src = (high + low) / 2.0
        upper_basic = src + self.factor * atr
        lower_basic = src - self.factor * atr

        if self._prev_upper is None or self._prev_lower is None:
            # First ATR-available bar. Pine `ta.supertrend` seeds direction := 1,
            # which in Pine means DOWNtrend (line = upperBand). In our flipped
            # convention (+1 up / -1 down) that is -1, so the seed line = upper.
            upper_band = upper_basic
            lower_band = lower_basic
            direction = -1                     # -1 == down (matches Pine seed)
        else:
            prev_close = self._prev_close if self._prev_close is not None else close
            upper_band = (upper_basic
                          if (upper_basic < self._prev_upper or prev_close > self._prev_upper)
                          else self._prev_upper)
            lower_band = (lower_basic
                          if (lower_basic > self._prev_lower or prev_close < self._prev_lower)
                          else self._prev_lower)
            if self._prev_supertrend == self._prev_upper:
                direction = 1 if close > upper_band else -1
            else:
                direction = -1 if close < lower_band else 1

        supertrend = lower_band if direction == 1 else upper_band

        self._prev_upper = upper_band
        self._prev_lower = lower_band
        self._prev_close = close
        self._prev_supertrend = supertrend
        return supertrend, direction


class DMI:
    """Wilder DMI/ADX, matching Pine `ta.dmi(di_len, adx_len)`.

    Returns (plus_di, minus_di, adx); adx is None until the ADX RMA is seeded.
    """

    def __init__(self, di_len: int, adx_len: int):
        self._tr = TrueRange()
        self._tr_rma = WilderSmoothing(di_len)
        self._plus_rma = WilderSmoothing(di_len)
        self._minus_rma = WilderSmoothing(di_len)
        self._adx_rma = WilderSmoothing(adx_len)
        self._prev_high: Optional[float] = None
        self._prev_low: Optional[float] = None

    def update(self, high: float, low: float, close: float):
        tr = self._tr.update(high, low, close)
        if self._prev_high is None:
            up = 0.0
            down = 0.0
        else:
            up = high - self._prev_high
            down = self._prev_low - low
        self._prev_high = high
        self._prev_low = low

        plus_dm = up if (up > down and up > 0) else 0.0
        minus_dm = down if (down > up and down > 0) else 0.0

        trur = self._tr_rma.update(tr)
        plus_s = self._plus_rma.update(plus_dm)
        minus_s = self._minus_rma.update(minus_dm)
        if trur is None or plus_s is None or minus_s is None or trur == 0:
            return None, None, None

        plus_di = 100.0 * plus_s / trur
        minus_di = 100.0 * minus_s / trur
        denom = plus_di + minus_di
        dx = 100.0 * abs(plus_di - minus_di) / (denom if denom != 0 else 1.0)
        adx = self._adx_rma.update(dx)
        return plus_di, minus_di, adx


class QualitySuperTrend:
    """Per-bar quality-filtered SuperTrend. Feed bars chronologically."""

    def __init__(self, config: Optional[QSTConfig] = None):
        self.cfg = config or QSTConfig()
        self._st = SuperTrend(self.cfg.factor, self.cfg.atr_period)
        self._dmi = DMI(self.cfg.adx_len, self.cfg.adx_len)
        self._ema_fast = EMA(self.cfg.ema_len)
        self._ema_slow = EMA(self.cfg.ema_slow_len)
        self._atr = WilderATR(self.cfg.atr_period)
        self._atr_avg = SMA(self.cfg.vol_avg_len)

    def update(
        self,
        timestamp: datetime,
        open_: float,
        high: float,
        low: float,
        close: float,
    ) -> QSTSnapshot:
        cfg = self.cfg
        st = self._st.update(high, low, close)
        plus_di, minus_di, adx = self._dmi.update(high, low, close)
        ema_fast = self._ema_fast.update(close)
        ema_slow = self._ema_slow.update(close)
        atr_now = self._atr.update(high, low, close)
        atr_avg = self._atr_avg.update(atr_now) if atr_now is not None else None

        if st is None:
            return QSTSnapshot(timestamp, None, 0, "GREY", adx, ema_fast, atr_now)

        supertrend, direction = st
        is_up = direction == 1
        st_dir = 1 if is_up else -1

        adx_ok = adx is not None and adx >= cfg.adx_thresh

        if ema_fast is None:
            ema_long_ok = ema_short_ok = False
        elif cfg.use_stack:
            ema_long_ok = ema_slow is not None and close > ema_fast and ema_fast > ema_slow
            ema_short_ok = ema_slow is not None and close < ema_fast and ema_fast < ema_slow
        else:
            ema_long_ok = close > ema_fast
            ema_short_ok = close < ema_fast

        vol_ok = (atr_now is not None and atr_avg is not None
                  and atr_now > atr_avg * cfg.vol_mult)

        quality_long = is_up and adx_ok and ema_long_ok and vol_ok
        quality_short = (not is_up) and adx_ok and ema_short_ok and vol_ok
        state = "GREEN" if quality_long else "RED" if quality_short else "GREY"

        return QSTSnapshot(timestamp, supertrend, st_dir, state, adx, ema_fast, atr_now)
