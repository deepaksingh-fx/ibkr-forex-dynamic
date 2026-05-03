"""
Streaming indicators. For now: 50-period EMA on 5-min closes (SPEC §11.4).

Convention:
    α = 2 / (N + 1)
    Seed: SMA of the first N closes; from bar N+1 onward, recursive EMA:
        EMA_t = α · close_t + (1 − α) · EMA_(t−1)

`update(close)` ingests one new bar's close and returns the new EMA value
(or None during warm-up before the seed completes).
"""
from __future__ import annotations

from collections import deque
from typing import Optional


class EMA:
    def __init__(self, period: int):
        if period < 2:
            raise ValueError("EMA period must be >= 2")
        self.period = period
        self.alpha = 2.0 / (period + 1)
        self._value: Optional[float] = None
        self._seed_buffer: deque[float] = deque(maxlen=period)

    @property
    def value(self) -> Optional[float]:
        """Current EMA value or None during warm-up."""
        return self._value

    @property
    def is_ready(self) -> bool:
        return self._value is not None

    def update(self, close: float) -> Optional[float]:
        """Ingest one new close. Returns the new EMA, or None during warm-up."""
        if self._value is None:
            # Still warming up: collect closes for the SMA seed.
            self._seed_buffer.append(close)
            if len(self._seed_buffer) == self.period:
                self._value = sum(self._seed_buffer) / self.period
                self._seed_buffer.clear()
            return self._value

        # Recursive EMA.
        self._value = self.alpha * close + (1.0 - self.alpha) * self._value
        return self._value

    def warmup(self, closes: list[float] | tuple[float, ...]) -> Optional[float]:
        """Convenience: ingest a batch of closes (e.g. historical pre-warm)."""
        last: Optional[float] = None
        for c in closes:
            last = self.update(c)
        return last
