"""Tests for cpr."""
from __future__ import annotations

import math

import pytest

from cpr import CPR, compute_cpr_from_bars, compute_cpr_from_hlc


# ------------------------------------------------------------------------
# compute_cpr_from_hlc - math
# ------------------------------------------------------------------------
class TestComputeCPRFromHLC:
    def test_basic_math(self):
        # H=1.1850, L=1.1700, C=1.1800
        c = compute_cpr_from_hlc(1.1850, 1.1700, 1.1800)
        # Pivot = (1.185 + 1.170 + 1.180) / 3 = 1.17833...
        assert math.isclose(c.pivot, 1.17833333, rel_tol=1e-6)
        # BC raw = (1.185 + 1.170) / 2 = 1.1775
        # TC raw = 2 * pivot - BC = 2 * 1.17833 - 1.1775 = 1.17917
        # TC > BC, no swap
        assert math.isclose(c.bc, 1.1775, rel_tol=1e-6)
        assert math.isclose(c.tc, 1.17917, rel_tol=1e-4)
        assert c.tc >= c.bc

    def test_tc_always_geq_bc_in_normal_case(self):
        c = compute_cpr_from_hlc(150.0, 145.0, 148.0)
        assert c.tc >= c.bc

    def test_force_ordering_when_close_below_bc(self):
        # When C < (H+L)/2, formula gives TC_raw < BC_raw -> must swap.
        # Pick H=10, L=1, C=2. BC_raw = 5.5. P = 13/3 ~= 4.333. TC_raw = 8.667 - 5.5 = 3.167.
        # 3.167 < 5.5 -> after swap: TC = 5.5, BC = 3.167.
        c = compute_cpr_from_hlc(10.0, 1.0, 2.0)
        assert c.tc >= c.bc
        assert math.isclose(c.tc, 5.5, rel_tol=1e-6)
        assert math.isclose(c.bc, 3.16667, rel_tol=1e-4)

    def test_degenerate_h_eq_l_eq_c(self):
        c = compute_cpr_from_hlc(1.0, 1.0, 1.0)
        assert c.pivot == 1.0
        assert c.bc == 1.0
        assert c.tc == 1.0
        assert c.width == 0.0
        assert c.width_pct == 0.0

    def test_h_less_than_l_raises(self):
        with pytest.raises(ValueError):
            compute_cpr_from_hlc(1.0, 2.0, 1.5)

    def test_negative_values_raise(self):
        with pytest.raises(ValueError):
            compute_cpr_from_hlc(-1.0, -2.0, -1.5)
        with pytest.raises(ValueError):
            compute_cpr_from_hlc(1.0, 0.5, -0.1)

    def test_zero_value_raises(self):
        with pytest.raises(ValueError):
            compute_cpr_from_hlc(0.0, 0.0, 0.0)

    def test_width_pct_property(self):
        c = compute_cpr_from_hlc(1.20, 1.10, 1.15)
        # width = tc - bc; pivot = 1.15; width_pct = width / pivot * 100
        expected_pct = c.width / c.pivot * 100.0
        assert math.isclose(c.width_pct, expected_pct, rel_tol=1e-9)

    def test_frozen(self):
        c = compute_cpr_from_hlc(1.20, 1.10, 1.15)
        with pytest.raises(Exception):
            c.high = 999  # type: ignore[misc]


# ------------------------------------------------------------------------
# compute_cpr_from_bars
# ------------------------------------------------------------------------
class TestComputeCPRFromBars:
    def test_multi_bar_aggregation(self):
        # Source window = 5 bars
        highs = [1.18, 1.19, 1.20, 1.185, 1.181]   # max 1.20
        lows = [1.17, 1.175, 1.168, 1.165, 1.16]    # min 1.16
        closes = [1.175, 1.18, 1.185, 1.179, 1.17]  # last 1.17
        c = compute_cpr_from_bars(highs, lows, closes)
        assert c.high == 1.20
        assert c.low == 1.16
        assert c.close == 1.17  # LAST bar's close

    def test_single_bar(self):
        c = compute_cpr_from_bars([1.18], [1.17], [1.175])
        assert c.high == 1.18 and c.low == 1.17 and c.close == 1.175

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            compute_cpr_from_bars([], [], [])

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            compute_cpr_from_bars([1.18, 1.19], [1.17], [1.175])

    def test_uses_last_close_not_max_close(self):
        # Even if max close > last close, we use last close (definition of C in CPR).
        c = compute_cpr_from_bars(
            highs=[1.20, 1.20, 1.20],
            lows=[1.10, 1.10, 1.10],
            closes=[1.19, 1.18, 1.11],   # last is lowest
        )
        assert c.close == 1.11
