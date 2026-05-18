"""
Streaming indicator helpers — Python ports of the Pine v6 `ta.*` functions
used by the regime classifier and adaptive SuperTrend.

All indicators are bar-by-bar stateful: feed one value at a time via
`.update(...)`, get back the current indicator value (or None during warm-up).

Functions implemented (with their Pine equivalents):
  - TrueRange         ~ ta.tr(true)
  - WilderSmoothing   ~ ta.rma(N)
  - WilderATR         ~ ta.atr(N)        = ta.rma(ta.tr(true), N)
  - SMA               ~ ta.sma(N)
  - EMA               ~ ta.ema(N)
  - Stdev             ~ ta.stdev(N)      (population — Pine default biased=true)
  - RSI               ~ ta.rsi(N)        (Wilder smoothing on gains/losses)
  - MACD              ~ ta.macd(fast, slow, signal)
  - PercentRank       ~ ta.percentrank(N)
  - ROC               ~ ta.roc(N)        = 100 * (src - src[N]) / src[N]
"""
from __future__ import annotations

from collections import deque
from typing import Optional, Tuple


class TrueRange:
    """Standard True Range using prev-bar close (Pine `ta.tr(true)`)."""

    def __init__(self):
        self._prev_close: Optional[float] = None

    def update(self, high: float, low: float, close: float) -> float:
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(
                high - low,
                abs(high - self._prev_close),
                abs(low - self._prev_close),
            )
        self._prev_close = close
        return tr


class WilderSmoothing:
    """
    RMA: seed = SMA of first N values, then recursive:
        rma_t = (rma_{t-1} * (N - 1) + x_t) / N
    Equivalent to EMA with alpha = 1/N.
    """

    def __init__(self, period: int):
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self._value: Optional[float] = None
        self._seed: list[float] = []

    def update(self, x: float) -> Optional[float]:
        if self._value is None:
            self._seed.append(x)
            if len(self._seed) == self.period:
                self._value = sum(self._seed) / self.period
                self._seed = []
            return self._value
        self._value = (self._value * (self.period - 1) + x) / self.period
        return self._value

    @property
    def value(self) -> Optional[float]:
        return self._value


class WilderATR:
    """ATR = RMA(TR, N)."""

    def __init__(self, period: int):
        self.period = period
        self._tr = TrueRange()
        self._rma = WilderSmoothing(period)

    def update(self, high: float, low: float, close: float) -> Optional[float]:
        tr = self._tr.update(high, low, close)
        return self._rma.update(tr)

    @property
    def value(self) -> Optional[float]:
        return self._rma.value


class SMA:
    """Simple moving average over the last N values."""

    def __init__(self, period: int):
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self._buf: deque[float] = deque(maxlen=period)
        self._sum: float = 0.0

    def update(self, x: float) -> Optional[float]:
        if len(self._buf) == self.period:
            self._sum -= self._buf[0]
        self._buf.append(x)
        self._sum += x
        if len(self._buf) < self.period:
            return None
        return self._sum / self.period

    @property
    def value(self) -> Optional[float]:
        if len(self._buf) < self.period:
            return None
        return self._sum / self.period


class EMA:
    """EMA with alpha = 2/(N+1). Seeded with SMA of first N values."""

    def __init__(self, period: int):
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self.alpha = 2.0 / (period + 1)
        self._value: Optional[float] = None
        self._seed: list[float] = []

    def update(self, x: float) -> Optional[float]:
        if self._value is None:
            self._seed.append(x)
            if len(self._seed) == self.period:
                self._value = sum(self._seed) / self.period
                self._seed = []
            return self._value
        self._value = self.alpha * x + (1.0 - self.alpha) * self._value
        return self._value

    @property
    def value(self) -> Optional[float]:
        return self._value


class Stdev:
    """Population standard deviation over last N values (matches Pine default)."""

    def __init__(self, period: int):
        if period < 2:
            raise ValueError("period must be >= 2")
        self.period = period
        self._buf: deque[float] = deque(maxlen=period)

    def update(self, x: float) -> Optional[float]:
        self._buf.append(x)
        if len(self._buf) < self.period:
            return None
        mean = sum(self._buf) / self.period
        var = sum((v - mean) ** 2 for v in self._buf) / self.period
        return var ** 0.5

    @property
    def value(self) -> Optional[float]:
        if len(self._buf) < self.period:
            return None
        mean = sum(self._buf) / self.period
        var = sum((v - mean) ** 2 for v in self._buf) / self.period
        return var ** 0.5


class RSI:
    """RSI with Wilder smoothing on gains/losses (Pine `ta.rsi` semantics)."""

    def __init__(self, period: int):
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self._avg_gain = WilderSmoothing(period)
        self._avg_loss = WilderSmoothing(period)
        self._prev: Optional[float] = None
        self._value: Optional[float] = None

    def update(self, close: float) -> Optional[float]:
        if self._prev is None:
            self._prev = close
            return None
        change = close - self._prev
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        ag = self._avg_gain.update(gain)
        al = self._avg_loss.update(loss)
        self._prev = close
        if ag is None or al is None:
            self._value = None
            return None
        if al == 0:
            self._value = 100.0
        else:
            rs = ag / al
            self._value = 100.0 - 100.0 / (1.0 + rs)
        return self._value

    @property
    def value(self) -> Optional[float]:
        return self._value


class MACD:
    """MACD: (line, signal, hist) — all None until warm-up completes."""

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        if fast >= slow:
            raise ValueError("fast must be < slow")
        self._ema_fast = EMA(fast)
        self._ema_slow = EMA(slow)
        self._ema_signal = EMA(signal)
        self._last: Tuple[Optional[float], Optional[float], Optional[float]] = (None, None, None)

    def update(self, close: float) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        ef = self._ema_fast.update(close)
        es = self._ema_slow.update(close)
        if ef is None or es is None:
            self._last = (None, None, None)
            return self._last
        line = ef - es
        sig = self._ema_signal.update(line)
        if sig is None:
            self._last = (line, None, None)
            return self._last
        hist = line - sig
        self._last = (line, sig, hist)
        return self._last


class PercentRank:
    """
    ta.percentrank(source, length): % of the previous `length` values that
    are strictly less than the current source. Returns 0..100, or None
    until at least `length` prior values exist.
    """

    def __init__(self, length: int):
        if length < 1:
            raise ValueError("length must be >= 1")
        self.length = length
        # Holds the `length` values BEFORE the current one (sliding window).
        self._buf: deque[float] = deque(maxlen=length)

    def update(self, x: float) -> Optional[float]:
        if len(self._buf) < self.length:
            self._buf.append(x)
            return None
        less = sum(1 for v in self._buf if v < x)
        rank = 100.0 * less / self.length
        self._buf.append(x)  # deque evicts oldest automatically
        return rank


class ROC:
    """ta.roc(source, length) = 100 * (source - source[length]) / source[length]."""

    def __init__(self, length: int):
        if length < 1:
            raise ValueError("length must be >= 1")
        self.length = length
        self._buf: deque[float] = deque(maxlen=length + 1)

    def update(self, x: float) -> Optional[float]:
        self._buf.append(x)
        if len(self._buf) < self.length + 1:
            return None
        ref = self._buf[0]
        if ref == 0:
            return 0.0
        return 100.0 * (x - ref) / ref
