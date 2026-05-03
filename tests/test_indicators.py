"""Tests for indicators (50-period EMA)."""
from __future__ import annotations

import math

import pytest

from indicators import EMA


class TestEMAConstruction:
    def test_period_one_raises(self):
        with pytest.raises(ValueError):
            EMA(1)

    def test_period_zero_raises(self):
        with pytest.raises(ValueError):
            EMA(0)

    def test_period_negative_raises(self):
        with pytest.raises(ValueError):
            EMA(-5)

    def test_alpha_for_period_50(self):
        ema = EMA(50)
        assert math.isclose(ema.alpha, 2 / 51, rel_tol=1e-9)

    def test_initial_state(self):
        ema = EMA(50)
        assert ema.value is None
        assert ema.is_ready is False


class TestEMAWarmup:
    def test_not_ready_during_warmup(self):
        ema = EMA(50)
        for i in range(49):
            v = ema.update(100.0)
            assert v is None
            assert ema.is_ready is False

    def test_ready_at_exactly_period_bars(self):
        ema = EMA(50)
        for i in range(49):
            ema.update(100.0)
        v = ema.update(100.0)  # 50th bar
        assert v == 100.0
        assert ema.is_ready is True

    def test_seed_is_sma_of_first_n(self):
        ema = EMA(5)
        # SMA of [10, 12, 14, 16, 18] = 14
        for c in [10, 12, 14, 16, 18]:
            ema.update(c)
        assert ema.value == 14.0


class TestEMARecursion:
    def test_constant_series_stays_constant(self):
        ema = EMA(10)
        for _ in range(50):
            ema.update(100.0)
        assert math.isclose(ema.value, 100.0, rel_tol=1e-9)

    def test_alpha_recursion_after_warmup(self):
        ema = EMA(50)
        # Warm up at 100.
        for _ in range(50):
            ema.update(100.0)
        # Feed 110. EMA = α·110 + (1-α)·100 = 2/51 * 110 + 49/51 * 100
        v = ema.update(110.0)
        expected = (2 / 51) * 110 + (49 / 51) * 100
        assert math.isclose(v, expected, rel_tol=1e-9)

    def test_trending_series_follows(self):
        # EMA should drift toward the new equilibrium when input keeps moving up.
        ema = EMA(10)
        for _ in range(10):
            ema.update(100.0)
        baseline = ema.value
        # Now feed many high values; EMA should rise toward 200.
        for _ in range(100):
            ema.update(200.0)
        assert ema.value > baseline
        assert ema.value > 199.0   # converged close to the new value

    def test_warmup_helper_batch(self):
        ema = EMA(50)
        closes = [100.0] * 50
        v = ema.warmup(closes)
        assert v == 100.0
        assert ema.is_ready is True

    def test_warmup_helper_partial(self):
        ema = EMA(50)
        v = ema.warmup([100.0] * 25)   # half the period
        assert v is None
        assert ema.is_ready is False
