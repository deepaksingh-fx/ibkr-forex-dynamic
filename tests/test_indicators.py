"""Unit tests for the streaming indicator helpers."""
from __future__ import annotations

import pytest

from indicators import (
    EMA,
    MACD,
    PercentRank,
    ROC,
    RSI,
    SMA,
    Stdev,
    TrueRange,
    WilderATR,
    WilderSmoothing,
)


# --- TrueRange ----------------------------------------------------------
class TestTrueRange:
    def test_first_bar_is_h_minus_l(self):
        tr = TrueRange()
        assert tr.update(110.0, 100.0, 105.0) == 10.0

    def test_uses_prev_close_for_gap(self):
        tr = TrueRange()
        tr.update(110.0, 100.0, 105.0)   # prev_close = 105
        # high=120, low=115, prev_close=105 -> max(5, 15, 10) = 15
        assert tr.update(120.0, 115.0, 118.0) == 15.0

    def test_low_below_prev_close(self):
        tr = TrueRange()
        tr.update(110.0, 100.0, 108.0)   # prev_close = 108
        # high=105, low=95, prev_close=108 -> max(10, |105-108|=3, |95-108|=13) = 13
        assert tr.update(105.0, 95.0, 100.0) == 13.0


# --- WilderSmoothing ----------------------------------------------------
class TestWilderSmoothing:
    def test_seed_is_sma_of_first_n(self):
        ws = WilderSmoothing(3)
        assert ws.update(10) is None
        assert ws.update(20) is None
        assert ws.update(30) == 20.0   # (10+20+30)/3

    def test_subsequent_uses_wilder_recursion(self):
        ws = WilderSmoothing(3)
        ws.update(10); ws.update(20); ws.update(30)   # seed = 20
        # next: (20 * 2 + 40) / 3 = 80/3 ~= 26.667
        out = ws.update(40)
        assert out == pytest.approx(26.666666666666668)


# --- WilderATR ----------------------------------------------------------
class TestWilderATR:
    def test_warms_up_after_period_bars(self):
        atr = WilderATR(3)
        # 3 bars to seed
        assert atr.update(110, 100, 105) is None  # tr=10
        assert atr.update(115, 105, 110) is None  # tr=max(10, |115-105|=10, |105-105|=0)=10
        out = atr.update(120, 110, 115)           # tr=max(10, |120-110|=10, |110-110|=0)=10
        assert out == 10.0   # SMA of (10, 10, 10)

    def test_recursive_after_seed(self):
        atr = WilderATR(3)
        for h, l, c in [(110, 100, 105), (115, 105, 110), (120, 110, 115)]:
            atr.update(h, l, c)
        # Now seeded with ATR=10. Next bar: tr=max(20, |130-115|=15, |110-115|=5)=20.
        # New ATR = (10*2 + 20)/3 = 40/3 ~= 13.333
        out = atr.update(130, 110, 120)
        assert out == pytest.approx(13.333333333333334)


# --- SMA ----------------------------------------------------------------
class TestSMA:
    def test_returns_none_before_period_filled(self):
        s = SMA(3)
        assert s.update(1) is None
        assert s.update(2) is None
        assert s.update(3) == 2.0

    def test_slides_correctly(self):
        s = SMA(3)
        s.update(1); s.update(2); s.update(3)   # = 2.0
        assert s.update(4) == 3.0   # (2+3+4)/3
        assert s.update(5) == 4.0


# --- EMA ----------------------------------------------------------------
class TestEMA:
    def test_seed_is_sma_of_first_n(self):
        e = EMA(3)
        e.update(10); e.update(20)
        assert e.update(30) == 20.0

    def test_recursive_step(self):
        e = EMA(3)
        for v in [10, 20, 30]:
            e.update(v)
        # alpha = 2/4 = 0.5. EMA = 0.5*40 + 0.5*20 = 30.
        assert e.update(40) == 30.0


# --- Stdev --------------------------------------------------------------
class TestStdev:
    def test_population_stdev(self):
        s = Stdev(3)
        s.update(2); s.update(4)
        # population stdev of (2,4,6): mean=4, var = ((4+0+4)/3)=8/3, sd=sqrt(8/3)~=1.6329
        out = s.update(6)
        assert out == pytest.approx((8.0 / 3.0) ** 0.5)

    def test_warmup(self):
        s = Stdev(3)
        assert s.update(1) is None
        assert s.update(2) is None
        assert s.update(3) is not None


# --- PercentRank --------------------------------------------------------
class TestPercentRank:
    def test_warmup_returns_none(self):
        pr = PercentRank(5)
        for v in [1, 2, 3, 4, 5]:
            assert pr.update(v) is None

    def test_rank_against_previous_window(self):
        pr = PercentRank(5)
        for v in [1, 2, 3, 4, 5]:
            pr.update(v)
        # Buffer is [1,2,3,4,5]. Current value 6 -> 5/5 less than -> 100%.
        assert pr.update(6) == 100.0

    def test_rank_50(self):
        pr = PercentRank(4)
        for v in [1, 2, 3, 4]:
            pr.update(v)
        # Buffer [1,2,3,4]. Current 2.5 -> 2/4 less than -> 50%.
        assert pr.update(2.5) == 50.0

    def test_lowest_rank(self):
        pr = PercentRank(4)
        for v in [10, 20, 30, 40]:
            pr.update(v)
        # Current 5 -> 0/4 less than -> 0%.
        assert pr.update(5) == 0.0


# --- ROC ----------------------------------------------------------------
class TestROC:
    def test_warmup(self):
        roc = ROC(3)
        for v in [100, 101, 102]:
            assert roc.update(v) is None
        # Buffer now full at length+1=4? No - only 3 so far. Need 4.
        # Wait - length=3 needs length+1=4 values.
        assert roc.update(103) is not None

    def test_roc_value(self):
        roc = ROC(3)
        for v in [100, 101, 102, 103]:
            out = roc.update(v)
        # On bar 4: source=103, source[3]=100 -> roc = 100*(103-100)/100 = 3.0
        assert out == pytest.approx(3.0)


# --- RSI ----------------------------------------------------------------
class TestRSI:
    def test_warmup(self):
        r = RSI(3)
        for v in [100, 101, 102, 103]:   # all gains
            r.update(v)
        # After 4 closes (3 changes), Wilder seed is ready. avg_loss=0 -> RSI=100.
        assert r.value == 100.0

    def test_rsi_zero_with_only_losses(self):
        r = RSI(3)
        for v in [100, 99, 98, 97]:
            r.update(v)
        # All losses -> RSI = 100 - 100/(1+0/x) = 0 (since avg_gain=0, rs=0)
        assert r.value == 0.0

    def test_rsi_bounded_zigzag(self):
        # Alternating gain/loss with small Wilder period oscillates within a
        # bounded band (the recursion never fully equilibrates because the
        # zigzag's phase aligns with the smoothing). We only assert the value
        # stays in a sane mid-range - sharp boundary checks above already
        # confirm the math.
        r = RSI(3)
        for i in range(80):
            r.update(100 if i % 2 == 0 else 102)
        assert 30.0 <= r.value <= 70.0


# --- MACD ---------------------------------------------------------------
class TestMACD:
    def test_returns_three_values(self):
        m = MACD(fast=3, slow=5, signal=2)
        out = m.update(100)
        assert isinstance(out, tuple) and len(out) == 3

    def test_eventually_emits_signal(self):
        m = MACD(fast=3, slow=5, signal=2)
        for v in [100, 101, 102, 103, 104, 105, 106]:
            line, sig, hist = m.update(v)
        assert line is not None
        assert sig is not None
        assert hist == pytest.approx(line - sig)
