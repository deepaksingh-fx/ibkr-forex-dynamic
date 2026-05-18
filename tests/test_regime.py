"""
Tests for the regime classifier (Pine v6 port).

Covers:
  - Config validation
  - ER computation matches the formula
  - Cross detection with PP shifts on FX-day rollover
  - Hysteresis behavior (enter > er_enter, leave < er_exit)
  - DIRECTIONAL_UP vs DIRECTIONAL_DOWN selection
  - MEAN_REVERTING vs NON_DIRECTIONAL based on crossings
  - Warming-up state during ER warmup
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from regime import Regime, RegimeClassifier, RegimeConfig


NY = ZoneInfo("America/New_York")


def _feed(clf, closes, pp=100.0, fx_day_start=None, start_ts=None, step_minutes=5):
    """Helper: feed a sequence of closes through the classifier, fixed PP/day."""
    if start_ts is None:
        start_ts = datetime(2026, 5, 12, 17, 0, tzinfo=NY)
    if fx_day_start is None:
        fx_day_start = datetime(2026, 5, 12, 17, 0, tzinfo=NY)
    snaps = []
    for i, c in enumerate(closes):
        ts = start_ts + timedelta(minutes=step_minutes * i)
        snaps.append(clf.update(ts, c, pp, fx_day_start))
    return snaps


# ────────────────────────────────────────────────────────────────────────
# Config validation
# ────────────────────────────────────────────────────────────────────────
class TestConfig:
    def test_defaults_match_pine(self):
        c = RegimeConfig()
        assert c.er_len == 20
        assert c.er_enter == 0.40
        assert c.er_exit == 0.30
        assert c.cross_len == 30
        assert c.cross_threshold == 3

    def test_er_exit_must_be_below_er_enter(self):
        with pytest.raises(ValueError, match="hysteresis"):
            RegimeConfig(er_enter=0.40, er_exit=0.40)
        with pytest.raises(ValueError, match="hysteresis"):
            RegimeConfig(er_enter=0.40, er_exit=0.50)

    def test_er_len_minimum(self):
        with pytest.raises(ValueError):
            RegimeConfig(er_len=1)

    def test_cross_threshold_minimum(self):
        with pytest.raises(ValueError):
            RegimeConfig(cross_threshold=0)


# ────────────────────────────────────────────────────────────────────────
# Warming up
# ────────────────────────────────────────────────────────────────────────
class TestWarmingUp:
    def test_first_n_bars_are_warming_up(self):
        clf = RegimeClassifier(RegimeConfig(er_len=5, cross_len=5, cross_threshold=2))
        snaps = _feed(clf, [100.0] * 5)
        # Need er_len + 1 closes before ER is computable.
        for s in snaps[:5]:
            assert s.regime == Regime.WARMING_UP
            assert s.er is None

    def test_er_emerges_on_bar_n_plus_1(self):
        clf = RegimeClassifier(RegimeConfig(er_len=5, cross_len=5, cross_threshold=2))
        # 6 closes total — first 5 warming, 6th has ER.
        snaps = _feed(clf, [100.0, 100.0, 100.0, 100.0, 100.0, 100.0])
        assert snaps[5].er == 0.0   # no movement at all → ER = 0
        assert snaps[5].regime != Regime.WARMING_UP


# ────────────────────────────────────────────────────────────────────────
# ER formula
# ────────────────────────────────────────────────────────────────────────
class TestEfficiencyRatio:
    def test_pure_trend_gives_er_1(self):
        # Each bar moves +1; sum of |Δ| = 5; |close - close[5]| = 5. ER = 1.0.
        clf = RegimeClassifier(RegimeConfig(er_len=5, cross_len=5, cross_threshold=2))
        snaps = _feed(clf, [100, 101, 102, 103, 104, 105])
        assert snaps[5].er == pytest.approx(1.0)

    def test_zero_movement_gives_er_0(self):
        clf = RegimeClassifier(RegimeConfig(er_len=5, cross_len=5, cross_threshold=2))
        snaps = _feed(clf, [100.0] * 6)
        assert snaps[5].er == 0.0

    def test_choppy_movement_low_er(self):
        # 100 → 105 → 100 → 105 → 100 → 105. Total |Δ| = 5*5=25. Net = |105-100|=5. ER = 5/25 = 0.2.
        clf = RegimeClassifier(RegimeConfig(er_len=5, cross_len=5, cross_threshold=2))
        snaps = _feed(clf, [100, 105, 100, 105, 100, 105])
        assert snaps[5].er == pytest.approx(0.2)


# ────────────────────────────────────────────────────────────────────────
# Hysteresis
# ────────────────────────────────────────────────────────────────────────
class TestHysteresis:
    def test_enters_at_er_enter(self):
        # Pure trend → ER = 1.0 > er_enter (0.40) → in_directional True.
        clf = RegimeClassifier(RegimeConfig(er_len=5, cross_len=5, cross_threshold=2))
        snaps = _feed(clf, [100, 101, 102, 103, 104, 105])
        assert snaps[5].in_directional is True
        assert snaps[5].regime == Regime.DIRECTIONAL_UP

    def test_stays_directional_between_exit_and_enter(self):
        # Get directional first, then drop ER into (er_exit, er_enter] band → stays.
        cfg = RegimeConfig(er_len=4, er_enter=0.40, er_exit=0.20, cross_len=5, cross_threshold=2)
        clf = RegimeClassifier(cfg)
        # Bars 0-4: pure trend, ER becomes 1.0 at bar 4. in_directional=True.
        snaps_trend = _feed(clf, [100, 101, 102, 103, 104])
        assert snaps_trend[4].in_directional is True

        # Now feed bars that drop ER into ~0.25 (between exit and enter).
        # Closes 104 → 103 → 104 → 103 → 102.
        # On bar 8 (5th additional): window covers bars 4-8 closes = [104,103,104,103,102].
        # Total |Δ| = 1+1+1+1 = 4. Net = |102 - 104| = 2. ER = 0.5 — still above enter.
        # Try a flatter sequence: 104,104,104,104,104 → ER = 0 → drops out.
        # I want ER between 0.20 and 0.40. Let me try 104,103,104,104,103,104:
        #   bars 4..8: closes=[104,103,104,104,103], Δ=[1,1,0,1], total=3, net=|103-104|=1, ER=0.33.
        more = _feed(clf, [103, 104, 104, 103],
                     start_ts=datetime(2026, 5, 12, 17, 25, tzinfo=NY))
        # Walk through; on bar 8 (last) ER should be ~0.33, which is in (exit, enter] → stays directional.
        assert 0.20 < (more[-1].er or 0) <= 0.40
        assert more[-1].in_directional is True

    def test_exits_when_er_below_er_exit(self):
        cfg = RegimeConfig(er_len=4, er_enter=0.40, er_exit=0.30, cross_len=5, cross_threshold=2)
        clf = RegimeClassifier(cfg)
        # Get directional.
        _feed(clf, [100, 101, 102, 103, 104])
        # Now feed a super-choppy sequence to drop ER below 0.30.
        # 104 → 100 → 104 → 100 → 104: huge Δ, zero net.
        # ER over the last 4 deltas = 0/16 = 0.
        more = _feed(clf, [100, 104, 100, 104],
                     start_ts=datetime(2026, 5, 12, 17, 25, tzinfo=NY))
        assert more[-1].in_directional is False


# ────────────────────────────────────────────────────────────────────────
# Direction
# ────────────────────────────────────────────────────────────────────────
class TestDirection:
    def test_direction_up_when_close_above_ref(self):
        clf = RegimeClassifier(RegimeConfig(er_len=5, cross_len=5, cross_threshold=2))
        snaps = _feed(clf, [100, 101, 102, 103, 104, 105])
        assert snaps[-1].regime == Regime.DIRECTIONAL_UP

    def test_direction_down_when_close_below_ref(self):
        clf = RegimeClassifier(RegimeConfig(er_len=5, cross_len=5, cross_threshold=2))
        snaps = _feed(clf, [105, 104, 103, 102, 101, 100])
        assert snaps[-1].regime == Regime.DIRECTIONAL_DOWN


# ────────────────────────────────────────────────────────────────────────
# PP crossings
# ────────────────────────────────────────────────────────────────────────
class TestCrossings:
    def test_count_increments_on_each_close_flip(self):
        # PP = 100. Closes: 99, 101, 99, 101, 99 → 4 sign flips after the first.
        clf = RegimeClassifier(RegimeConfig(er_len=2, cross_len=5, cross_threshold=2))
        snaps = _feed(clf, [99, 101, 99, 101, 99], pp=100.0)
        # bar 0: no prev → cross=0
        # bar 1: prev_diff = -1 (99<100), cur_diff = +1 (101>100) → cross=1
        # bar 2: prev_diff +, cur -, → cross=1
        # bar 3: prev -, cur + → cross=1
        # bar 4: prev +, cur - → cross=1
        # Total = 4 crossings in 5 bars.
        assert snaps[-1].crossings == 4

    def test_rollover_bar_does_not_count_as_cross(self):
        clf = RegimeClassifier(RegimeConfig(er_len=2, cross_len=5, cross_threshold=2))
        # First FX day: close=99, pp=100.
        d1_start = datetime(2026, 5, 12, 17, 0, tzinfo=NY)
        d2_start = datetime(2026, 5, 13, 17, 0, tzinfo=NY)
        clf.update(d1_start, 99.0, 100.0, d1_start)
        # FX-day rollover: PP shifts to 95, close jumps to 96 — would naively look
        # like a cross (99 above old PP 100? no — 99 below 100; 96 above new PP 95
        # — so the diff sign flipped). With newDay guard, must NOT count.
        snap = clf.update(d2_start, 96.0, 95.0, d2_start)
        assert snap.crossings == 0


# ────────────────────────────────────────────────────────────────────────
# Classification thresholds
# ────────────────────────────────────────────────────────────────────────
class TestClassification:
    def test_mean_reverting_when_crossings_above_threshold(self):
        # Want NOT directional but many crossings.
        # ER will be ~0.2 (oscillating). Crossings will be 4 (as above).
        clf = RegimeClassifier(RegimeConfig(
            er_len=5, er_enter=0.40, er_exit=0.30,
            cross_len=5, cross_threshold=3,
        ))
        snaps = _feed(clf, [99, 101, 99, 101, 99, 101], pp=100.0)
        assert snaps[-1].in_directional is False
        assert snaps[-1].crossings >= 3
        assert snaps[-1].regime == Regime.MEAN_REVERTING

    def test_non_directional_when_neither(self):
        # Flat closes, all on one side of PP → ER=0, crossings=0.
        clf = RegimeClassifier(RegimeConfig(
            er_len=5, er_enter=0.40, er_exit=0.30,
            cross_len=5, cross_threshold=3,
        ))
        snaps = _feed(clf, [99.0] * 6, pp=100.0)
        assert snaps[-1].in_directional is False
        assert snaps[-1].crossings == 0
        assert snaps[-1].regime == Regime.NON_DIRECTIONAL
