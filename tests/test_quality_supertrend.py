"""Tests for quality_supertrend (classic SuperTrend + ADX/EMA/vol filters)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from quality_supertrend import DMI, QualitySuperTrend, QSTConfig, SuperTrend


def _ts(i: int) -> datetime:
    return datetime(2026, 1, 5, tzinfo=timezone.utc) + timedelta(minutes=5 * i)


def _feed_trend(qst: QualitySuperTrend, n: int, start: float, step: float):
    """Feed n bars trending by `step` per bar with a small HL spread."""
    snaps = []
    close = start
    prev_close = start
    for i in range(n):
        close = start + step * i
        hi = close + abs(step) * 0.6 + 0.05
        lo = close - abs(step) * 0.6 - 0.05
        snaps.append(qst.update(_ts(i), prev_close, hi, lo, close))
        prev_close = close
    return snaps


class TestSuperTrend:
    def test_warmup_returns_none(self):
        st = SuperTrend(factor=2.0, atr_period=10)
        # First atr_period-1 bars: ATR not ready -> None.
        out = [st.update(100 + i, 100 + i + 0.5, 100 + i - 0.5) for i in range(5)]
        assert all(o is None for o in out)

    def test_uptrend_direction_positive(self):
        st = SuperTrend(factor=2.0, atr_period=10)
        last_dir = None
        for i in range(60):
            c = 100 + i * 0.5
            res = st.update(c + 0.3, c - 0.3, c)
            if res is not None:
                _, last_dir = res
        assert last_dir == 1   # +1 == uptrend

    def test_downtrend_direction_negative(self):
        st = SuperTrend(factor=2.0, atr_period=10)
        last_dir = None
        for i in range(60):
            c = 100 - i * 0.5
            res = st.update(c + 0.3, c - 0.3, c)
            if res is not None:
                _, last_dir = res
        assert last_dir == -1


class TestDMI:
    def test_adx_eventually_available_and_bounded(self):
        dmi = DMI(14, 14)
        adx = None
        for i in range(120):
            c = 100 + i * 0.4
            _, _, adx = dmi.update(c + 0.3, c - 0.3, c)
        assert adx is not None
        assert 0.0 <= adx <= 100.0

    def test_strong_trend_high_adx(self):
        dmi = DMI(14, 14)
        adx = None
        for i in range(150):
            c = 100 + i * 0.5
            _, _, adx = dmi.update(c + 0.2, c - 0.2, c)
        assert adx is not None and adx > 18.0


class TestQualitySuperTrend:
    def test_warmup_is_grey_dir_zero(self):
        qst = QualitySuperTrend()
        s = qst.update(_ts(0), 100, 100.2, 99.8, 100)
        assert s.st_dir == 0
        assert s.state == "GREY"

    def test_strong_uptrend_goes_green(self):
        qst = QualitySuperTrend()
        snaps = _feed_trend(qst, 160, start=100.0, step=0.5)
        assert snaps[-1].st_dir == 1
        assert snaps[-1].state == "GREEN"

    def test_strong_downtrend_goes_red(self):
        qst = QualitySuperTrend()
        snaps = _feed_trend(qst, 160, start=200.0, step=-0.5)
        assert snaps[-1].st_dir == -1
        assert snaps[-1].state == "RED"

    def test_flat_market_is_grey(self):
        qst = QualitySuperTrend()
        last = None
        for i in range(160):
            last = qst.update(_ts(i), 100.0, 100.0001, 99.9999, 100.0)
        # No volatility / no trend -> filters fail -> grey.
        assert last.state == "GREY"
