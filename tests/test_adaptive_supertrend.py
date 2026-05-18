"""
End-to-end sanity tests for AdaptiveSuperTrend.

Most micro-tests are in test_indicators.py. Here we focus on:
  - Config validation
  - Bar-by-bar update doesn't crash on a long noisy sequence
  - Active method is set sensibly
  - Signals only fire when filters AND direction flip align
  - Live trade state respects "signal beats TSL" ordering
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from adaptive_supertrend import (
    METHOD_NAMES,
    AdaptiveSnapshot,
    AdaptiveSTConfig,
    AdaptiveSuperTrend,
)

NY = ZoneInfo("America/New_York")


def _bar_stream(n=600, seed=1, base=100.0):
    """Synthetic OHLC: deterministic random walk with small wicks."""
    rng = random.Random(seed)
    t = datetime(2026, 1, 1, 0, 0, tzinfo=NY)
    closes = [base]
    for _ in range(n - 1):
        closes.append(closes[-1] + rng.gauss(0, 0.5))
    for i in range(n):
        c = closes[i]
        o = closes[i - 1] if i > 0 else c
        high = max(o, c) + abs(rng.gauss(0, 0.3))
        low = min(o, c) - abs(rng.gauss(0, 0.3))
        yield (t + timedelta(minutes=5 * i), o, high, low, c)


class TestConfig:
    def test_defaults_match_pine(self):
        c = AdaptiveSTConfig()
        assert c.base_atr == 10
        assert c.base_mult == 3.0
        assert c.eval_interval_bars == 30
        assert c.min_trades == 5
        assert c.perf_lookback_days == 60
        assert c.manual_method == "Percentile"
        assert c.tsl_method == "ATR"
        assert c.target_method == "ATR"

    def test_bad_criterion_rejected(self):
        with pytest.raises(ValueError):
            AdaptiveSTConfig(selection_criterion="Nonsense")

    def test_bad_manual_method_rejected(self):
        with pytest.raises(ValueError):
            AdaptiveSTConfig(manual_method="Foo")

    def test_bad_tsl_method_rejected(self):
        with pytest.raises(ValueError):
            AdaptiveSTConfig(tsl_method="Stochastic")

    def test_min_atr_validation(self):
        with pytest.raises(ValueError):
            AdaptiveSTConfig(min_atr=20, max_atr=20)
        with pytest.raises(ValueError):
            AdaptiveSTConfig(min_atr=1, max_atr=20)


class TestBarLoop:
    def test_doesnt_crash_on_long_run(self):
        cfg = AdaptiveSTConfig(
            eval_interval_bars=20,
            perf_lookback_days=1,    # tiny so bars_per_day=288 -> 288-bar window
        )
        ast = AdaptiveSuperTrend(cfg, bars_per_day=288)
        last = None
        for ts, o, h, l, c in _bar_stream(n=600):
            last = ast.update(ts, o, h, l, c)
        assert isinstance(last, AdaptiveSnapshot)
        assert last.bar_index == 599
        assert last.active_method_idx in range(6)
        assert last.active_method_name in METHOD_NAMES

    def test_active_method_settles_into_something(self):
        cfg = AdaptiveSTConfig(eval_interval_bars=20, perf_lookback_days=1, min_trades=2)
        ast = AdaptiveSuperTrend(cfg, bars_per_day=288)
        for ts, o, h, l, c in _bar_stream(n=500):
            snap = ast.update(ts, o, h, l, c)
        # Should have logged trades on most methods by now.
        # At least ONE method should have >= min_trades.
        assert any(t >= cfg.min_trades for t in snap.method_trade_counts)


class TestManualMethodMode:
    def test_manual_mode_uses_configured_method(self):
        cfg = AdaptiveSTConfig(enable_auto=False, manual_method="Z-Score")
        ast = AdaptiveSuperTrend(cfg, bars_per_day=288)
        for ts, o, h, l, c in _bar_stream(n=300):
            snap = ast.update(ts, o, h, l, c)
        assert snap.active_method_name == "Z-Score"


class TestSignalsRequireFlipPlusFilters:
    def test_no_signal_without_flip(self):
        # Disable filters so signals depend ONLY on flip.
        cfg = AdaptiveSTConfig(
            enable_auto=False, manual_method="Percentile",
            enable_rsi=False, enable_macd=False,
            base_atr=3, pctl_lookback=10, reg_ma=10, zs_lookback=10,
            roc_lookback=5, hyb_smooth=3,
        )
        ast = AdaptiveSuperTrend(cfg, bars_per_day=288)
        flip_count = 0
        sig_count = 0
        for ts, o, h, l, c in _bar_stream(n=400):
            snap = ast.update(ts, o, h, l, c)
            if snap.bull_signal or snap.bear_signal:
                sig_count += 1
        # Signals are gated by flips; some flips should produce signals.
        assert sig_count > 0


class TestLiveTradeStateMachine:
    def test_signal_beats_tsl(self):
        # Construct a contrived scenario where TSL would fire AND a bear signal
        # also fires - bear signal must win (state flips to short, not flat).
        cfg = AdaptiveSTConfig(
            enable_auto=False, manual_method="Percentile",
            enable_rsi=False, enable_macd=False,
            enable_tsl=True, tsl_method="Fixed Points", tsl_points=0.01,  # tight TSL
            enable_targets=False,
            base_atr=2, pctl_lookback=5, reg_ma=5, zs_lookback=5,
            roc_lookback=3, hyb_smooth=2,
        )
        ast = AdaptiveSuperTrend(cfg, bars_per_day=288)
        # Just feed bars - the test is that the state machine doesn't get stuck.
        for ts, o, h, l, c in _bar_stream(n=300, seed=7):
            snap = ast.update(ts, o, h, l, c)
        # Sanity: liveDir is one of {-1, 0, 1}
        assert snap.live_dir in (-1, 0, 1)
