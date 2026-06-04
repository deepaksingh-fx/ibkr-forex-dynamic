"""
Tests for the coil-index pair selection.

Logic under test:
  1. ADX filter: only pairs with adx_now < adx_filter_max (30) survive.
  2. coil_index = (1 - low_adx_frac) * width_pct, width_pct = (TC-BC)/TC*100.
  3. Lowest coil_index wins; ties -> first appearance.
"""
from __future__ import annotations

import math

import pytest

from coil_selection import (
    ADX_COIL_MAX,
    PairCoil,
    coil_select,
    compute_pair_coil,
)
from cpr import compute_cpr_from_hlc
from selection import SelectionError


def _pc(symbol, adx_now, *, H, L, C, low, total, plus=25.0, minus=10.0):
    return PairCoil(
        symbol=symbol,
        adx_now=adx_now,
        plus_di=plus,
        minus_di=minus,
        cpr=compute_cpr_from_hlc(H, L, C),
        low_adx_bars=low,
        total_adx_bars=total,
    )


class _Bar:
    __slots__ = ("high", "low", "close")

    def __init__(self, high, low, close):
        self.high = high
        self.low = low
        self.close = close


class TestCoilIndexMath:
    def test_width_pct_uses_tc_denominator(self):
        # Close skewed from (H+L)/2 so pivot != tc and the two denominators diverge.
        cpr = compute_cpr_from_hlc(101.0, 99.0, 100.8)
        expected = (cpr.tc - cpr.bc) / cpr.tc * 100.0
        assert PairCoil("X", 20.0, 25.0, 10.0, cpr, 0, 288).width_pct == pytest.approx(expected)
        # and it differs from the pivot-denominator width used by the old strategy
        assert cpr.width_pct_tc != pytest.approx(cpr.width_pct, rel=1e-6)

    def test_coil_index_formula(self):
        m = _pc("X", 20.0, H=100.20, L=100.00, C=100.10, low=216, total=288)
        assert m.low_adx_frac == pytest.approx(216 / 288)
        assert m.coil_index == pytest.approx((1 - 216 / 288) * m.width_pct)

    def test_more_low_adx_bars_lowers_index(self):
        wide = _pc("A", 20.0, H=101.0, L=99.0, C=100.5, low=50, total=288)
        coiled = _pc("B", 20.0, H=101.0, L=99.0, C=100.5, low=250, total=288)
        assert coiled.coil_index < wide.coil_index

    def test_zero_valid_bars_raises(self):
        with pytest.raises(SelectionError):
            _pc("X", 20.0, H=100.2, L=100.0, C=100.1, low=0, total=0).low_adx_frac


class TestCoilSelect:
    def test_lowest_coil_index_wins(self):
        a = _pc("AUDJPY", 22.0, H=101.0, L=99.0, C=100.5, low=50, total=288)   # wide
        b = _pc("NZDJPY", 18.0, H=100.2, L=100.0, C=100.1, low=250, total=288)  # coiled
        assert coil_select([a, b]).symbol == "NZDJPY"

    def test_adx_filter_drops_trending_pairs(self):
        trending = _pc("USDJPY", 31.0, H=100.1, L=100.0, C=100.05, low=280, total=288)  # best geom
        calm = _pc("EURUSD", 25.0, H=101.0, L=99.0, C=100.5, low=10, total=288)          # worse geom
        # USDJPY would win on coil index but is filtered out by ADX >= 30.
        assert coil_select([trending, calm]).symbol == "EURUSD"

    def test_all_trending_raises(self):
        a = _pc("USDJPY", 30.0, H=100.1, L=100.0, C=100.05, low=1, total=288)
        b = _pc("EURUSD", 45.0, H=100.1, L=100.0, C=100.05, low=1, total=288)
        with pytest.raises(SelectionError, match="ADX"):
            coil_select([a, b])

    def test_empty_raises(self):
        with pytest.raises(SelectionError):
            coil_select([])

    def test_tie_breaks_to_first_appearance(self):
        a = _pc("FIRST", 20.0, H=100.2, L=100.0, C=100.1, low=100, total=288)
        b = _pc("SECOND", 20.0, H=100.2, L=100.0, C=100.1, low=100, total=288)
        assert coil_select([a, b]).symbol == "FIRST"
        assert coil_select([b, a]).symbol == "SECOND"


class TestComputePairCoil:
    def _trend_bars(self, n):
        # Clean uptrend -> DI+ dominates, ADX climbs high.
        out = []
        for i in range(n):
            base = 100.0 + i * 0.05
            out.append(_Bar(base + 0.02, base - 0.02, base))
        return out

    def test_builder_on_trend_has_high_adx_now(self):
        cpr = compute_cpr_from_hlc(100.2, 100.0, 100.1)
        m = compute_pair_coil("TREND", self._trend_bars(350), cpr)
        assert m.adx_now > 30.0                       # clean trend -> trending
        assert m.total_adx_bars == 288                # window capped at 24h
        assert 0 <= m.low_adx_bars <= m.total_adx_bars
        assert m.plus_di > m.minus_di                 # uptrend
        assert m.cpr is cpr

    def test_builder_window_caps_at_288(self):
        cpr = compute_cpr_from_hlc(100.2, 100.0, 100.1)
        m = compute_pair_coil("X", self._trend_bars(500), cpr)
        assert m.total_adx_bars == 288

    def test_builder_raises_when_adx_never_seeds(self):
        cpr = compute_cpr_from_hlc(100.2, 100.0, 100.1)
        with pytest.raises(SelectionError, match="seed"):
            compute_pair_coil("X", self._trend_bars(10), cpr)

    def test_builder_raises_on_empty(self):
        cpr = compute_cpr_from_hlc(100.2, 100.0, 100.1)
        with pytest.raises(SelectionError):
            compute_pair_coil("X", [], cpr)

    def test_coil_max_default(self):
        assert ADX_COIL_MAX == 20.0
